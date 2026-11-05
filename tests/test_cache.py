"""Testes do cache de cotações: TTL, stale-if-error e proteção contra stampede."""

import asyncio

import pytest

from app.cache import QuoteCache
from app.provider import PairNotFound, Quote, UpstreamError

QUOTE = Quote(sell=5.4, buy=5.39, date="2026-11-05 10:00:00")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fetcher:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.fail = False
        self.delay = delay

    async def __call__(self, src, dst):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise UpstreamError("fora do ar")
        return QUOTE


def run(coro):
    return asyncio.run(coro)


def test_hit_dentro_do_ttl():
    fetch, clock = Fetcher(), Clock()
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600, clock=clock)

    async def scenario():
        await cache.get("USD", "BRL")
        clock.now += 29
        await cache.get("USD", "BRL")

    run(scenario())
    assert fetch.calls == 1


def test_expira_depois_do_ttl():
    fetch, clock = Fetcher(), Clock()
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600, clock=clock)

    async def scenario():
        await cache.get("USD", "BRL")
        clock.now += 31
        await cache.get("USD", "BRL")

    run(scenario())
    assert fetch.calls == 2


def test_ttl_zero_desliga_o_cache():
    fetch = Fetcher()
    cache = QuoteCache(fetch, ttl=0, stale_ttl=0)

    async def scenario():
        for _ in range(3):
            await cache.get("USD", "BRL")

    run(scenario())
    assert fetch.calls == 3


def test_pares_diferentes_nao_se_misturam():
    fetch = Fetcher()
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600)

    async def scenario():
        await cache.get("USD", "BRL")
        await cache.get("EUR", "BRL")

    run(scenario())
    assert fetch.calls == 2


def test_stale_if_error():
    fetch, clock = Fetcher(), Clock()
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600, clock=clock)

    async def scenario():
        await cache.get("USD", "BRL")
        clock.now += 120
        fetch.fail = True
        return await cache.get("USD", "BRL")

    assert run(scenario()) == QUOTE


def test_stale_vencido_propaga_erro():
    fetch, clock = Fetcher(), Clock()
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600, clock=clock)

    async def scenario():
        await cache.get("USD", "BRL")
        clock.now += 601
        fetch.fail = True
        await cache.get("USD", "BRL")

    with pytest.raises(UpstreamError):
        run(scenario())


def test_par_inexistente_nao_e_cacheado():
    async def not_found(src, dst):
        raise PairNotFound(src + dst)

    cache = QuoteCache(not_found, ttl=30, stale_ttl=600)
    with pytest.raises(PairNotFound):
        run(cache.get("XXX", "BRL"))


def test_stampede_so_uma_chamada_externa():
    """100 requisições simultâneas com o cache vazio -> 1 chamada à API externa."""
    fetch = Fetcher(delay=0.05)
    cache = QuoteCache(fetch, ttl=30, stale_ttl=600)

    async def scenario():
        return await asyncio.gather(*(cache.get("USD", "BRL") for _ in range(100)))

    results = run(scenario())
    assert all(q == QUOTE for q in results)
    assert fetch.calls == 1
