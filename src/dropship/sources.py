from __future__ import annotations

from typing import Protocol, runtime_checkable

from dropship.models import Product


@runtime_checkable
class ProductSource(Protocol):
    """A best-sellers data source (KeepaClient, CanopyClient)."""

    def bestsellers(self, category) -> list[str]: ...

    def products(self, asins: list[str]) -> list[Product]: ...
