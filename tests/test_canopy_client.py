import json
from pathlib import Path

import httpx
import pytest

from dropship.canopy_client import CanopyClient, CanopyError
from dropship.config import Settings, load_config
from dropship.errors import DataSourceError
from dropship.keepa_client import KeepaClient, KeepaError
from dropship.sources import ProductSource

FIXTURE = Path(__file__).parent / "fixtures" / "canopy_bestsellers.json"


def make(handler, key="SECRETKEY"):
    return CanopyClient(key, "US", http=httpx.Client(
        base_url="https://rest.canopyapi.co", transport=httpx.MockTransport(handler)))


def ok_handler(seen=None):
    def h(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
    return h


def test_canopy_bestsellers_maps_rows():
    c = make(ok_handler())
    assert c.bestsellers("bestsellers_kitchen") == ["B0CANOPY01", "B0CANOPY02", "B0CANOPY03"]
    p = c.products(["B0CANOPY01"])[0]
    assert (p.asin, p.title) == ("B0CANOPY01", "Silicone Spatula Set")
    assert p.price == 29.99 and p.rating == 4.6 and p.review_count == 1000
    assert p.sales_rank == 1 and p.offer_count is None
    assert p.price_avg90 is None and p.sales_rank_avg90 is None


def test_canopy_products_from_cache_no_request():
    seen = []
    c = make(ok_handler(seen))
    c.bestsellers("x")
    assert len(seen) == 1
    got = c.products(["B0CANOPY03", "UNKNOWN", "B0CANOPY01"])
    assert [p.asin for p in got] == ["B0CANOPY03", "B0CANOPY01"]
    assert len(seen) == 1


def test_canopy_missing_price():
    c = make(ok_handler())
    c.bestsellers("x")
    by = {p.asin: p for p in c.products(["B0CANOPY02", "B0CANOPY03"])}
    assert by["B0CANOPY02"].price is None
    assert by["B0CANOPY03"].price is None


def test_canopy_missing_rating_is_none():
    body = {"data": {"amazonBestSellers": {"productResults": {"results": [
        {"asin": "B0X", "price": {"value": 10}}]}}}}
    c = make(lambda r: httpx.Response(200, json=body))
    c.bestsellers("x")
    p = c.products(["B0X"])[0]
    assert p.rating is None and p.review_count is None
    assert p.title == ""


def test_canopy_missing_rank_falls_back_to_position():
    body = {"data": {"amazonBestSellers": {"productResults": {"results": [
        {"asin": "B0X", "bestSellersRank": 7},
        {"asin": "B0Y"},
        {"title": "no asin, still counts as a position"},
        {"asin": "B0Z", "bestSellersRank": None},
    ]}}}}
    c = make(lambda r: httpx.Response(200, json=body))
    c.bestsellers("x")
    by = {p.asin: p.sales_rank for p in c.products(["B0X", "B0Y", "B0Z"])}
    assert by == {"B0X": 7, "B0Y": 2, "B0Z": 4}


def test_canopy_sends_key_header_and_params():
    seen = []
    make(ok_handler(seen)).bestsellers("bestsellers_kitchen")
    r = seen[0]
    assert r.headers["API-KEY"] == "SECRETKEY"
    assert r.url.path == "/api/amazon/bestsellers"
    assert r.url.params["categoryId"] == "bestsellers_kitchen"
    assert r.url.params["domain"] == "US"
    assert r.url.params["page"] == "1"
    assert "SECRETKEY" not in str(r.url)


def test_canopy_http_500_raises():
    c = make(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(CanopyError) as ei:
        c.bestsellers("x")
    assert ei.value.status_code == 500
    assert isinstance(ei.value, DataSourceError)
    assert "HTTP 500" in str(ei.value)


@pytest.mark.parametrize("body", [[1, 2], {}, {"data": {}}, {"data": {"amazonBestSellers": None}}])
def test_canopy_bad_shape_raises(body):
    c = make(lambda r: httpx.Response(200, json=body))
    with pytest.raises(CanopyError, match="unexpected response shape") as ei:
        c.bestsellers("x")
    assert ei.value.status_code is None


def test_canopy_key_not_in_errors():
    c = make(lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(CanopyError) as ei:
        c.bestsellers("x")
    assert "SECRETKEY" not in str(ei.value)
    assert "SECRETKEY" not in repr(ei.value)
    assert ei.value.__cause__ is None


def test_keepa_error_is_data_source_error():
    assert issubclass(KeepaError, DataSourceError)


def test_us_demo_defaults(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('categories = ["bestsellers_kitchen", "12"]\n', encoding="utf-8")
    s = load_config(cfg)
    assert s.data_source == "canopy" and s.canopy_domain == "US"
    assert s.price_max == 60.0 and s.price_min == 15.0
    assert s.max_order_value == 300.0 and s.daily_spend_cap == 1000.0
    assert s.keepa_domain == 1 and s.max_sales_rank == 50
    assert s.shipping_cost == 5.0 and s.max_asins_per_category == 50
    assert s.categories == ["bestsellers_kitchen", "12"]
    assert Settings(categories=["a"]).categories == ["a"]


def test_canopy_bestsellers_cached_per_category():
    seen = []
    c = make(ok_handler(seen))
    first = c.bestsellers("x")
    assert c.bestsellers("x") == first
    assert len(seen) == 1
    c.bestsellers("y")
    assert len(seen) == 2


def test_canopy_failed_request_not_cached():
    seen = []
    ok = ok_handler()

    def h(request):
        seen.append(request)
        return httpx.Response(503) if len(seen) == 1 else ok(request)

    c = make(h)
    with pytest.raises(CanopyError):
        c.bestsellers("x")
    assert c.bestsellers("x") == ["B0CANOPY01", "B0CANOPY02", "B0CANOPY03"]
    assert len(seen) == 2
    c.bestsellers("x")
    assert len(seen) == 2


def test_canopy_api_errors_body_raises_without_key():
    msg = "Invalid categoryId: " + "x" * 300
    body = {"data": None, "errors": [{"message": msg}, {"message": "second"}]}
    c = make(lambda r: httpx.Response(200, json=body))
    with pytest.raises(CanopyError) as ei:
        c.bestsellers("x")
    assert str(ei.value) == "API error: " + msg[:200]
    assert "SECRETKEY" not in str(ei.value)
    assert ei.value.status_code is None


def test_canopy_api_errors_never_echo_key():
    body = {"errors": [{"message": "bad key SECRETKEY"}]}
    c = make(lambda r: httpx.Response(200, json=body))
    with pytest.raises(CanopyError) as ei:
        c.bestsellers("x")
    assert "SECRETKEY" not in str(ei.value)
    assert str(ei.value).startswith("API error: ")


def test_canopy_empty_results_is_not_an_error():
    body = {"data": {"amazonBestSellers": {"productResults": {"results": []}}}}
    assert make(lambda r: httpx.Response(200, json=body)).bestsellers("x") == []


def test_both_clients_are_product_sources():
    assert isinstance(make(ok_handler()), ProductSource)
    assert isinstance(KeepaClient("k", 1, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)))), ProductSource)
