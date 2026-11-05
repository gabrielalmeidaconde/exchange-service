"""Exchange: cotação entre moedas para usuários autenticados.

Contrato (docs/arquitetura.md do platform):

    GET /exchange/{from}/{to}     header id-account (injetado pelo gateway)
    -> { "sell": float, "buy": float, "date": "YYYY-MM-DD HH:MM:SS", "id-account": "..." }

A cotação vem de uma API de terceiros (AwesomeAPI). Bottleneck tratado aqui:
cada chamada externa custa ~100-500 ms e a API tem limite de requisições, então
guardamos a cotação em memória por CACHE_TTL segundos (ver cache.py).
"""

import os
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
from starlette.routing import Match

from .cache import QuoteCache
from .provider import AwesomeApiProvider, PairNotFound, UpstreamError

CURRENCY = re.compile(r"^[A-Za-z]{3}$")

HTTP_SECONDS = Histogram(
    "exchange_http_server_requests_seconds",
    "Tempo de resposta das rotas do exchange",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Fábrica do app. Os testes passam um transport falso no lugar da API externa."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        timeout = float(os.getenv("UPSTREAM_TIMEOUT", "3"))
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            provider = AwesomeApiProvider(
                client,
                base_url=os.getenv("EXCHANGE_API_URL", "https://economia.awesomeapi.com.br/json/last"),
                token=os.getenv("EXCHANGE_API_TOKEN") or None,
            )
            app.state.cache = QuoteCache(
                provider.fetch,
                ttl=float(os.getenv("CACHE_TTL", "30")),
                stale_ttl=float(os.getenv("CACHE_STALE_TTL", "600")),
            )
            yield

    app = FastAPI(title="exchange", lifespan=lifespan, docs_url="/exchange/docs",
                  openapi_url="/exchange/openapi.json", redoc_url=None)

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # usa o template da rota ("/exchange/{from_}/{to}") e não a URL real,
            # para não criar uma série no Prometheus por par de moedas
            path = next((r.path for r in app.routes if r.matches(request.scope)[0] == Match.FULL), "desconhecida")
            if path != "/metrics":
                HTTP_SECONDS.labels(request.method, path, str(status)).observe(time.perf_counter() - start)

    @app.get("/exchange/health-check")
    @app.get("/health")
    async def health():
        return {"status": "UP"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        # só na rede interna: o gateway só encaminha /exchange/** e o lb não expõe o exchange
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/exchange/{from_}/{to}")
    async def exchange(
        from_: str,
        to: str,
        request: Request,
        id_account: str | None = Header(default=None, alias="id-account"),
    ):
        # o gateway sempre injeta o id-account depois de validar o JWT; sem ele a
        # requisição não passou pelo gateway
        if not id_account:
            raise HTTPException(status_code=401, detail="header id-account ausente")
        if not CURRENCY.match(from_) or not CURRENCY.match(to):
            raise HTTPException(status_code=400, detail="moeda deve ter 3 letras (ISO 4217), ex.: USD")

        src, dst = from_.upper(), to.upper()
        if src == dst:
            return {"sell": 1.0, "buy": 1.0, "date": time.strftime("%Y-%m-%d %H:%M:%S"), "id-account": id_account}

        try:
            quote = await request.app.state.cache.get(src, dst)
        except PairNotFound:
            raise HTTPException(status_code=404, detail=f"par {src}-{dst} não suportado")
        except UpstreamError:
            raise HTTPException(status_code=503, detail="API de câmbio indisponível, tente de novo")

        return {"sell": quote.sell, "buy": quote.buy, "date": quote.date, "id-account": id_account}

    return app


app = create_app()
