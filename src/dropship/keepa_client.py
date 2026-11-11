from __future__ import annotations

import httpx

from dropship.errors import DataSourceError
from dropship.models import Product

# Indexes into Keepa's stats.current / stats.avg90 arrays.
IDX_NEW_PRICE = 1
IDX_SALES_RANK = 3
IDX_COUNT_NEW = 11
IDX_RATING = 16
IDX_COUNT_REVIEWS = 17

CHUNK_SIZE = 100
DEFAULT_REFILL_MS = 60000


class KeepaError(DataSourceError):
    pass


class KeepaOutOfTokens(KeepaError):
    def __init__(self, message: str, refill_in_ms: int, status_code: int | None = None):
        super().__init__(message, status_code)
        self.refill_in_ms = refill_in_ms


def _val(arr, idx: int) -> int | None:
    """Raw stat value, or None if the array/index is missing or the value is -1."""
    if not isinstance(arr, list) or idx >= len(arr):
        return None
    v = arr[idx]
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
        return None
    return int(v)


def _scaled(arr, idx: int, divisor: int) -> float | None:
    v = _val(arr, idx)
    return None if v is None else v / divisor


def _to_product(raw: dict) -> Product:
    stats = raw.get("stats") or {}
    cur = stats.get("current")
    avg = stats.get("avg90")
    return Product(
        asin=raw["asin"],
        title=raw.get("title") or "",
        price=_scaled(cur, IDX_NEW_PRICE, 100),
        price_avg90=_scaled(avg, IDX_NEW_PRICE, 100),
        sales_rank=_val(cur, IDX_SALES_RANK),
        sales_rank_avg90=_val(avg, IDX_SALES_RANK),
        rating=_scaled(cur, IDX_RATING, 10),
        review_count=_val(cur, IDX_COUNT_REVIEWS),
        offer_count=_val(cur, IDX_COUNT_NEW),
    )


class KeepaClient:
    def __init__(self, api_key: str, domain: int, http: httpx.Client | None = None):
        self._key = api_key
        self._domain = domain
        self._http = http or httpx.Client(base_url="https://api.keepa.com", timeout=30)
        self.requests_made = 0  # HTTP requests attempted, failed ones included

    def _get(self, path: str, params: dict) -> dict:
        # Network errors (httpx.TimeoutException / ConnectError) propagate unchanged;
        # their str() does not include the URL/query string.
        self.requests_made += 1
        resp = self._http.get(path, params={"key": self._key, "domain": self._domain, **params})
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        if status == 429:
            raise KeepaOutOfTokens(
                f"GET {path} -> 429 (out of tokens)",
                refill_in_ms=int(body.get("refillIn") or DEFAULT_REFILL_MS),
                status_code=429,
            )
        if not 200 <= status < 300:
            raise KeepaError(f"GET {path} -> HTTP {status}", status_code=status)
        return body

    @staticmethod
    def _check_tokens(path: str, body: dict, has_data: bool) -> None:
        tokens = body.get("tokensLeft")
        if isinstance(tokens, (int, float)) and tokens <= 0 and not has_data:
            raise KeepaOutOfTokens(
                f"GET {path}: out of tokens",
                refill_in_ms=int(body.get("refillIn") or DEFAULT_REFILL_MS),
            )

    def bestsellers(self, category: int) -> list[str]:
        body = self._get("/bestsellers", {"category": category})
        asins = (body.get("bestSellersList") or {}).get("asinList")
        self._check_tokens("/bestsellers", body, bool(asins))
        return list(asins or [])

    def products(self, asins: list[str]) -> list[Product]:
        out: list[Product] = []
        for i in range(0, len(asins), CHUNK_SIZE):
            chunk = asins[i : i + CHUNK_SIZE]
            body = self._get("/product", {"asin": ",".join(chunk), "stats": 90})
            raw = body.get("products")
            self._check_tokens("/product", body, bool(raw))
            out.extend(_to_product(p) for p in raw or [])
        return out
