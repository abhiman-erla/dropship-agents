from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


class Settings(BaseModel):
    keepa_domain: int = 10  # 10 = amazon.in, 1 = amazon.com
    categories: list[int] = []  # Keepa category ids; required
    price_min: float = 300.0
    price_max: float = 3000.0
    max_sales_rank: int = 50000
    min_rating: float = 4.0
    min_reviews: int = 50
    max_offer_count: int = 15
    fee_pct: float = 0.15
    shipping_cost: float = 60.0
    est_cost_ratio: float = 0.35
    min_margin_pct: float = 0.25
    llm_top_n: int = 20
    daily_llm_token_budget: int = 200000
    max_risk_score: int = 60
    max_order_value: float = 5000.0
    daily_spend_cap: float = 15000.0
    max_units_per_order: int = 20
    db_path: Path = Path("data/dropship.db")
    reports_dir: Path = Path("reports")
    supplier_quotes_path: Path = Path("supplier_quotes.csv")


def load_config(path: Path) -> Settings:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    settings = Settings(**data)
    if not settings.categories:
        raise ConfigError("config: `categories` must list at least one Keepa category id")
    return settings
