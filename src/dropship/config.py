from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    """Raised when the configuration is invalid."""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_source: Literal["keepa", "canopy"] = "canopy"
    canopy_domain: str = "US"
    keepa_domain: int = 1  # 1 = amazon.com, 10 = amazon.in
    # Keepa category ids (int) or Canopy category ids (str); required
    categories: list[int | str] = Field(default_factory=list)
    price_min: float = Field(15.0, ge=0)
    price_max: float = Field(60.0, gt=0)
    # for Canopy the best-seller list position is the rank
    max_sales_rank: int = Field(50, gt=0)
    min_rating: float = Field(4.0, ge=0, le=5)
    min_reviews: int = Field(50, ge=0)
    max_offer_count: int = Field(15, gt=0)
    fee_pct: float = Field(0.15, ge=0, lt=1)
    shipping_cost: float = Field(5.0, ge=0)
    est_cost_ratio: float = Field(0.35, ge=0, lt=1)
    min_margin_pct: float = Field(0.25, ge=0, lt=1)
    llm_top_n: int = Field(20, gt=0)
    daily_llm_token_budget: int = Field(200000, gt=0)
    max_risk_score: int = Field(60, ge=0, le=100)
    max_order_value: float = Field(300.0, gt=0)
    daily_spend_cap: float = Field(1000.0, gt=0)
    max_units_per_order: int = Field(20, gt=0)
    # best-seller lists are rank-ordered; keeps data-source use bounded
    max_asins_per_category: int = Field(50, gt=0)
    # an approved/rejected ASIN is not proposed again for this many days (0 = off)
    proposal_cooldown_days: int = Field(30, ge=0)
    db_path: Path = Path("data/dropship.db")
    reports_dir: Path = Path("reports")
    supplier_quotes_path: Path = Path("supplier_quotes.csv")

    @model_validator(mode="after")
    def _categories_match_source(self) -> Settings:
        if self.data_source == "canopy":
            bad = [c for c in self.categories if not isinstance(c, str)]
            kind = "a string (Canopy category id)"
        else:
            bad = [c for c in self.categories if not isinstance(c, int) or isinstance(c, bool)]
            kind = "an integer (Keepa category id)"
        if bad:
            raise ValueError(
                f'categories: with data_source = "{self.data_source}" every category must be '
                f"{kind}; got {', '.join(repr(c) for c in bad)}"
            )
        return self


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
        raise ConfigError("config: `categories` must list at least one category id")
    return settings
