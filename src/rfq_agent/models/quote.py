from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from .drawing import DrawingSpec
from .pipeline import (
    BOMLine,
    ConfidenceReport,
    CostBreakdown,
    RoutingOp,
    SimilarPart,
    ValidationIssue,
)
from .rfq import RFQItem, RFQRequest


class LineQuoteDraft(BaseModel):
    line_no: int
    item: RFQItem
    spec: DrawingSpec
    render_paths: list[str]
    issues: list[ValidationIssue]
    similar_parts: list[SimilarPart]
    bom: list[BOMLine]
    routing: list[RoutingOp]
    uncovered_features: list[str]
    costs: list[CostBreakdown]
    recommended_plant: str | None
    price_deviation_pct: float | None
    confidence: ConfidenceReport


class QuoteDraft(BaseModel):
    rfq_id: str
    version: int
    request: RFQRequest
    lines: list[LineQuoteDraft]
    assumptions: list[str]
    margin_pct: float


class ReviewEdit(BaseModel):
    line_no: int
    target: Literal["routing", "margin", "plant"]
    seq: int | None = None
    field: str  # cycle_min / setup_min / add_op / remove_op / accept_suggested / margin_pct / plant
    new_value: Any = None


class ReviewDecision(BaseModel):
    action: Literal["approve", "edit", "reject", "request_clarification"]
    reviewer: str
    edits: list[ReviewEdit] = []
    comment: str | None = None


class QuoteLinePrice(BaseModel):
    qty: int
    unit_price_eur: float
    total_price_eur: float
    option: bool = False  # extra standard tier offered as an option (quoting policy: quantity tiers)
    min_order_applied: bool = False


class QuoteLine(BaseModel):
    line_no: int
    part_number: str | None
    description: str | None
    plant: str
    prices: list[QuoteLinePrice]
    lead_time_weeks: int
    unpriced_items: list[str] = []
    customer_part_number: str | None = None


class Quote(BaseModel):
    quote_id: str
    rfq_id: str
    customer_name: str
    contact_name: str | None
    language: str
    issued_on: date
    valid_until: date
    currency: str
    incoterm: str | None
    lines: list[QuoteLine]
    assumptions: list[str]
    cover_letter: str
    approved_by: str
    approved_at: datetime
    version: int = 1
    review_comment: str | None = None
    cover_letter_source: str = "template"  # "llm" or "template: <reason>"


class FeedbackRecord(BaseModel):
    rfq_id: str
    line_no: int
    reviewer: str
    edits: list[ReviewEdit]
    before: dict
    after: dict
    created_at: datetime
    diff: list[dict] = []  # field-level changes: {"path", "before", "after"}


class TraceEvent(BaseModel):
    node: str
    started_at: datetime
    duration_ms: int
    llm_calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    summary: str = ""
