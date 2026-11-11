from __future__ import annotations

import httpx

from dropship.errors import DataSourceError
from dropship.models import Product

PATH = "/api/amazon/bestsellers"


class CanopyError(DataSourceError):
    pass


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _to_product(row: dict, position: int) -> Product:
    price = row.get("price")
    value = _num(price.get("value")) if isinstance(price, dict) else None
    rating = _num(row.get("rating"))
    reviews = _num(row.get("ratingsTotal"))
    rank = _num(row.get("bestSellersRank"))
    if rank is None:
        rank = position  # the list is rank-ordered; 1-based position is the rank
    return Product(
        asin=row["asin"],
        title=row.get("title") or "",
        price=float(value) if value else None,
        price_avg90=None,
        sales_rank=int(rank),
        sales_rank_avg90=None,
        rating=None if rating is None else float(rating),
        review_count=None if reviews is None else int(reviews),
        offer_count=None,
    )


class CanopyClient:
    """Best-sellers source backed by the Canopy REST API (one request per category)."""

    def __init__(self, api_key: str, domain: str = "US", http: httpx.Client | None = None):
        self._key = api_key
        self._domain = domain
        self._http = http or httpx.Client(base_url="https://rest.canopyapi.co", timeout=30)
        self._cache: dict[str, Product] = {}
        self.requests_made = 0  # HTTP requests attempted, failed ones included
        # One request per category per client (i.e. per run), even across stage retries.
        self._lists: dict[str, list[str]] = {}

    def _api_error(self, errors: list) -> CanopyError:
        first = errors[0]
        msg = first.get("message") if isinstance(first, dict) else None
        text = str(msg if msg is not None else first)
        if self._key:
            text = text.replace(self._key, "***")
        return CanopyError(f"API error: {text[:200]}", status_code=None)

    def bestsellers(self, category: str) -> list[str]:
        if category in self._lists:
            return list(self._lists[category])
        # Network errors propagate unchanged; the key travels in a header, not the URL.
        self.requests_made += 1
        resp = self._http.get(
            PATH,
            params={"categoryId": category, "domain": self._domain, "page": 1},
            headers={"API-KEY": self._key},
        )
        if not 200 <= resp.status_code < 300:
            raise CanopyError(f"GET {PATH} -> HTTP {resp.status_code}",
                              status_code=resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            body = None
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            raise self._api_error(errors)
        rows = None
        if isinstance(body, dict):
            data = body.get("data")
            best = data.get("amazonBestSellers") if isinstance(data, dict) else None
            results = best.get("productResults") if isinstance(best, dict) else None
            rows = results.get("results") if isinstance(results, dict) else None
        if not isinstance(rows, list):
            raise CanopyError("unexpected response shape", status_code=None)
        asins: list[str] = []
        for position, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or not row.get("asin"):
                continue
            product = _to_product(row, position)
            self._cache[product.asin] = product
            asins.append(product.asin)
        self._lists[category] = asins
        return list(asins)

    def products(self, asins: list[str]) -> list[Product]:
        return [self._cache[a] for a in asins if a in self._cache]
