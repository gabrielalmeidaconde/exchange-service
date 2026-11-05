"""Testes do contrato GET /exchange/{from}/{to} com a API externa simulada."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

AWESOME_USD_BRL = {
    "USDBRL": {
        "code": "USD",
        "codein": "BRL",
        "bid": "5.3912",
        "ask": "5.3942",
        "create_date": "2026-11-05 10:00:00",
    }
}


class FakeApi:
    """Substitui a AwesomeAPI e conta quantas vezes foi chamada."""

    def __init__(self, status=200, body=None, fail=False):
        self.calls = 0
        self.status = status
        self.body = AWESOME_USD_BRL if body is None else body
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectTimeout("timeout", request=request)
        if request.url.path.endswith("/XXX-BRL"):
            return httpx.Response(404, json={"status": 404, "code": "CoinNotExists"})
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def client(api):
    with TestClient(create_app(transport=httpx.MockTransport(api))) as c:
        yield c


def get(client, path, account="42"):
    headers = {"id-account": account} if account else {}
    return client.get(path, headers=headers)


def test_contrato(client):
    r = get(client, "/exchange/USD/BRL")
    assert r.status_code == 200
    assert r.json() == {"sell": 5.3942, "buy": 5.3912, "date": "2026-11-05 10:00:00", "id-account": "42"}


def test_sell_maior_ou_igual_buy(client):
    body = get(client, "/exchange/USD/BRL").json()
    assert body["sell"] >= body["buy"]


def test_moeda_minuscula_vira_maiuscula(client, api):
    assert get(client, "/exchange/usd/brl").status_code == 200


def test_sem_id_account_401(client):
    assert get(client, "/exchange/USD/BRL", account=None).status_code == 401


def test_moeda_invalida_400(client, api):
    assert get(client, "/exchange/US/BRL").status_code == 400
    assert get(client, "/exchange/USD1/BRL").status_code == 400
    assert api.calls == 0


def test_par_inexistente_404(client):
    assert get(client, "/exchange/XXX/BRL").status_code == 404


def test_mesma_moeda_nao_chama_api(client, api):
    r = get(client, "/exchange/BRL/BRL")
    assert r.status_code == 200
    assert r.json()["sell"] == r.json()["buy"] == 1.0
    assert api.calls == 0


def test_cache_evita_chamadas_repetidas(client, api):
    for _ in range(50):
        assert get(client, "/exchange/USD/BRL").status_code == 200
    assert api.calls == 1


def test_api_fora_do_ar_sem_cache_503():
    with TestClient(create_app(transport=httpx.MockTransport(FakeApi(fail=True)))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503


def test_resposta_inesperada_503():
    with TestClient(create_app(transport=httpx.MockTransport(FakeApi(body={"erro": 1})))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503


def test_limite_da_api_429_vira_503():
    with TestClient(create_app(transport=httpx.MockTransport(FakeApi(status=429)))) as c:
        assert get(c, "/exchange/USD/BRL").status_code == 503


def test_health(client):
    assert client.get("/health").status_code == 200
    assert client.get("/exchange/health-check").status_code == 200


def test_metricas(client):
    get(client, "/exchange/USD/BRL")
    get(client, "/exchange/USD/BRL")
    text = client.get("/metrics").text
    assert 'exchange_quote_cache_total{result="hit"}' in text
    assert 'route="/exchange/{from_}/{to}"' in text
    # a URL real não vira label (cardinalidade)
    assert "/exchange/USD/BRL" not in text
