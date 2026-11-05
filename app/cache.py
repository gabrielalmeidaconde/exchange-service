"""Bottleneck: cache da cotação.

Sem cache, cada GET /exchange/USD/BRL vira uma chamada HTTP para a API externa:
latência de rede de terceiros no caminho de toda requisição e risco de bater no
limite de requisições da API (que derruba o serviço para todo mundo).

Decisões:
  - cache em memória por par, TTL curto (CACHE_TTL, padrão 30 s): cotação de
    30 s atrás é aceitável para uma loja, e a cardinalidade é minúscula
    (poucos pares de moedas), então não precisa de Redis;
  - um lock por par: com 200 usuários pedindo USD-BRL no mesmo instante em que
    o cache expira, só UMA chamada vai para a API externa (evita "cache stampede");
  - se a API externa falhar, devolve a última cotação conhecida por até
    CACHE_STALE_TTL (padrão 10 min) em vez de 503 ("stale-if-error").
"""

import asyncio
import time
from typing import Awaitable, Callable

from prometheus_client import Counter

from .provider import PairNotFound, Quote, UpstreamError

CACHE_RESULTS = Counter(
    "exchange_quote_cache",
    "Resultado da consulta ao cache de cotações",
    ["result"],  # hit | miss | stale | error
)

Fetcher = Callable[[str, str], Awaitable[Quote]]


class QuoteCache:
    def __init__(self, fetch: Fetcher, ttl: float, stale_ttl: float, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._ttl = ttl
        self._stale_ttl = stale_ttl
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[Quote, float]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _fresh(self, key: tuple[str, str]) -> Quote | None:
        entry = self._entries.get(key)
        if entry and self._clock() - entry[1] < self._ttl:
            return entry[0]
        return None

    async def get(self, src: str, dst: str) -> Quote:
        key = (src, dst)
        if self._ttl > 0 and (quote := self._fresh(key)):
            CACHE_RESULTS.labels("hit").inc()
            return quote

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # outra requisição pode ter atualizado o cache enquanto esperávamos o lock
            if self._ttl > 0 and (quote := self._fresh(key)):
                CACHE_RESULTS.labels("hit").inc()
                return quote

            try:
                quote = await self._fetch(src, dst)
            except PairNotFound:
                CACHE_RESULTS.labels("miss").inc()
                raise
            except UpstreamError:
                entry = self._entries.get(key)
                if entry and self._clock() - entry[1] < self._stale_ttl:
                    CACHE_RESULTS.labels("stale").inc()
                    return entry[0]
                CACHE_RESULTS.labels("error").inc()
                raise

            CACHE_RESULTS.labels("miss").inc()
            self._entries[key] = (quote, self._clock())
            return quote
