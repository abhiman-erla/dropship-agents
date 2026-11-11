from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Product(BaseModel):
    asin: str
    title: str
    price: float | None
    price_avg90: float | None
    sales_rank: int | None
    sales_rank_avg90: int | None
    rating: float | None
    review_count: int | None
    offer_count: int | None


class Candidate(BaseModel):
    asin: str
    title: str
    sell_price: float
    supplier_cost: float
    cost_source: Literal["quoted", "estimated"]
    margin: float
    margin_pct: float
    score: float
    llm_verdict: Literal["promising", "weak", "avoid", "unreviewed"] = "unreviewed"
    llm_reason: str = ""


class RiskAssessment(BaseModel):
    asin: str
    risk_score: int
    factors: dict[str, int]
    narrative: str = ""
    suggested_qty: int = 0


class Proposal(BaseModel):
    id: int | None
    run_id: int
    asin: str
    qty: int
    unit_cost: float
    total: float
    risk_score: int
    narrative: str
    status: Literal["pending", "approved_manual", "rejected"]


class StageResult(BaseModel):
    stage: str
    status: Literal["ok", "degraded", "failed", "skipped"]
    attempts: int
    error_type: str = ""
    message: str = ""
