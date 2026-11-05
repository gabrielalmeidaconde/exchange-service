"""Cliente da API de câmbio de terceiros (AwesomeAPI, gratuita e sem cadastro).

    GET https://economia.awesomeapi.com.br/json/last/USD-BRL
    -> {"USDBRL": {"bid": "5.39", "ask": "5.40", "create_date": "2026-11-05 10:00:00", ...}}

"bid" é o preço pelo qual o mercado compra a moeda (buy) e "ask" o preço pelo
qual vende (sell); por isso sell >= buy.
"""

import time
from dataclasses import dataclass

import httpx
from prometheus_client import Histogram

UPSTREAM_SECONDS = Histogram(
    "exchange_upstream_request_seconds",
    "Tempo das chamadas à API de câmbio externa",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5),
)


@dataclass(frozen=True)
class Quote:
    sell: float
    buy: float
    date: str


class PairNotFound(Exception):
    """A API externa não conhece o par de moedas."""


class UpstreamError(Exception):
    """A API externa falhou (timeout, 5xx, limite de requisições, resposta inválida)."""


class AwesomeApiProvider:
    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str | None = None):
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def fetch(self, src: str, dst: str) -> Quote:
        params = {"token": self._token} if self._token else None
        start = time.perf_counter()
        outcome = "error"
        try:
            try:
                resp = await self._client.get(f"{self._base_url}/{src}-{dst}", params=params)
            except httpx.HTTPError as e:
                raise UpstreamError(str(e)) from e

            if resp.status_code == 404:
                outcome = "not_found"
                raise PairNotFound(f"{src}-{dst}")
            if resp.status_code != 200:
                raise UpstreamError(f"status {resp.status_code}")

            try:
                data = resp.json()[f"{src}{dst}"]
                quote = Quote(sell=float(data["ask"]), buy=float(data["bid"]), date=data["create_date"])
            except (ValueError, KeyError, TypeError) as e:
                raise UpstreamError(f"resposta inesperada: {e}") from e

            outcome = "ok"
            return quote
        finally:
            UPSTREAM_SECONDS.labels(outcome).observe(time.perf_counter() - start)
