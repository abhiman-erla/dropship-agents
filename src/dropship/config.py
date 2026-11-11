from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keepa_domain: int = 10  # 10 = amazon.in, 1 = amazon.com
    categories: list[int] = Field(default_factory=list)  # Keepa category ids; required
    price_min: float = Field(300.0, ge=0)
    price_max: float = Field(3000.0, gt=0)
    max_sales_rank: int = Field(50000, gt=0)
    min_rating: float = Field(4.0, ge=0, le=5)
    min_reviews: int = Field(50, ge=0)
    max_offer_count: int = Field(15, gt=0)
    fee_pct: float = Field(0.15, ge=0, lt=1)
    shipping_cost: float = Field(60.0, ge=0)
    est_cost_ratio: float = Field(0.35, ge=0, lt=1)
    min_margin_pct: float = Field(0.25, ge=0, lt=1)
    llm_top_n: int = Field(20, gt=0)
    daily_llm_token_budget: int = Field(200000, gt=0)
    max_risk_score: int = Field(60, ge=0, le=100)
    max_order_value: float = Field(5000.0, gt=0)
    daily_spend_cap: float = Field(15000.0, gt=0)
    max_units_per_order: int = Field(20, gt=0)
    # best-seller lists are rank-ordered; keeps Keepa token use bounded
    max_asins_per_category: int = Field(100, gt=0)
    db_path: Path = Path("data/dropship.db")
    reports_dir: Path = Path("reports")
    supplier_quotes_path: Path = Path("supplier_quotes.csv")


def _describe(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "config"
        parts.append(f"`{loc}`: {err['msg']}")
    return "; ".join(parts)


def load_config(path: Path) -> Settings:
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config: {path} is not valid TOML: {exc}") from exc
    try:
        settings = Settings(**data)
    except ValidationError as exc:
        raise ConfigError(f"config: {_describe(exc)}") from exc
    if not settings.categories:
        raise ConfigError("config: `categories` must list at least one Keepa category id")
    return settings
