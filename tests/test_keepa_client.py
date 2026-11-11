import json
from pathlib import Path

import httpx
import pytest

from dropship.keepa_client import KeepaClient, KeepaError, KeepaOutOfTokens

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def make(handler, key="SECRETKEY"):
    http = httpx.Client(base_url="https://api.keepa.com", transport=httpx.MockTransport(handler))
    return KeepaClient(key, 1, http=http)


def test_bestsellers_parses_asins():
    c = make(lambda r: httpx.Response(200, json=load("keepa_bestsellers.json")))
    assert c.bestsellers(3760911) == ["B0A", "B0B", "B0C"]


def test_bestsellers_sends_params():
    seen = []

    def h(r):
        seen.append(r)
        return httpx.Response(200, json=load("keepa_bestsellers.json"))

    make(h).bestsellers(7)
    assert seen[0].url.path == "/bestsellers"
    assert seen[0].url.params["category"] == "7"
    assert seen[0].url.params["domain"] == "1"
    assert seen[0].url.params["key"] == "SECRETKEY"


def test_products_maps_fields():
    c = make(lambda r: httpx.Response(200, json=load("keepa_products.json")))
    p = c.products(["B0A", "B0B", "B0C"])[0]
    assert p.asin == "B0A"
    assert p.title == "Widget Alpha"
    assert p.price == 499.0
    assert p.price_avg90 == 520.0
    assert p.sales_rank == 1500
    assert p.sales_rank_avg90 == 1800
    assert p.rating == 4.3
    assert p.review_count == 812
    assert p.offer_count == 6


def test_minus_one_becomes_none():
    c = make(lambda r: httpx.Response(200, json=load("keepa_products.json")))
    p = c.products(["B0A", "B0B", "B0C"])[1]
    assert p.price is None and p.price_avg90 is None
    assert p.sales_rank is None and p.rating is None and p.review_count is None
    assert p.offer_count == 2


def test_short_array_and_missing_title():
    c = make(lambda r: httpx.Response(200, json=load("keepa_products.json")))
    p = c.products(["B0A", "B0B", "B0C"])[2]
    assert p.title == ""
    assert p.price == 29.99
    assert p.price_avg90 is None
    assert p.rating is None and p.offer_count is None


def test_missing_stats_maps_to_none():
    body = {"tokensLeft": 5, "products": [{"asin": "X", "title": "t"}]}
    p = make(lambda r: httpx.Response(200, json=body)).products(["X"])[0]
    assert p.price is None and p.sales_rank is None


def test_chunking():
    calls = []

    def h(r):
        calls.append(r)
        n = len(r.url.params["asin"].split(","))
        return httpx.Response(200, json={"tokensLeft": 9, "products": [{"asin": f"A{i}"} for i in range(n)]})

    out = make(h).products([f"A{i}" for i in range(250)])
    assert len(calls) == 3
    assert [len(r.url.params["asin"].split(",")) for r in calls] == [100, 100, 50]
    assert all(r.url.params["stats"] == "90" for r in calls)
    assert len(out) == 250


def test_empty_products_makes_no_request():
    def h(r):
        raise AssertionError("no request expected")

    assert make(h).products([]) == []


def test_out_of_tokens():
    c = make(lambda r: httpx.Response(200, json={"tokensLeft": -5, "refillIn": 42000}))
    with pytest.raises(KeepaOutOfTokens) as ei:
        c.products(["B0A"])
    assert ei.value.refill_in_ms == 42000


def test_out_of_tokens_bestsellers():
    c = make(lambda r: httpx.Response(200, json={"tokensLeft": 0, "refillIn": 1000}))
    with pytest.raises(KeepaOutOfTokens):
        c.bestsellers(1)


def test_zero_tokens_with_data_is_ok():
    body = load("keepa_bestsellers.json")
    body["tokensLeft"] = 0
    assert make(lambda r: httpx.Response(200, json=body)).bestsellers(1) == ["B0A", "B0B", "B0C"]


def test_http_429_default_refill():
    c = make(lambda r: httpx.Response(429, json={}))
    with pytest.raises(KeepaOutOfTokens) as ei:
        c.bestsellers(1)
    assert ei.value.refill_in_ms == 60000
    assert ei.value.status_code == 429


def test_http_429_body_refill():
    c = make(lambda r: httpx.Response(429, json={"refillIn": 5000}))
    with pytest.raises(KeepaOutOfTokens) as ei:
        c.bestsellers(1)
    assert ei.value.refill_in_ms == 5000


def test_http_500():
    c = make(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(KeepaError) as ei:
        c.bestsellers(1)
    assert ei.value.status_code == 500
    assert not isinstance(ei.value, KeepaOutOfTokens)


def test_api_key_not_in_exception_text():
    c = make(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(KeepaError) as ei:
        c.bestsellers(1)
    assert "SECRETKEY" not in str(ei.value)
    assert "SECRETKEY" not in repr(ei.value)
    assert ei.value.__cause__ is None


def test_network_errors_propagate_without_key_in_text():
    def h(r):
        raise httpx.ConnectError("connection failed", request=r)

    def t(r):
        raise httpx.ReadTimeout("timed out", request=r)

    for handler, exc in ((h, httpx.ConnectError), (t, httpx.TimeoutException)):
        with pytest.raises(exc) as ei:
            make(handler).bestsellers(1)
        assert "SECRETKEY" not in str(ei.value)


def test_default_client_construction():
    c = KeepaClient("k", 1)
    assert str(c._http.base_url).startswith("https://api.keepa.com")
