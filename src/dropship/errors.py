from __future__ import annotations


class DataSourceError(Exception):
    """Failure talking to a product data source (Keepa, Canopy)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
