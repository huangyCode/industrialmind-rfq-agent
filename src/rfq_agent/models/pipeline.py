"""Models produced by the deterministic pipeline steps (review, similarity, BOM, routing, costing, confidence)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .enums import MakeOrBuy, Severity, Tier

Plant = Literal["DE", "PL", "CN"]


class ValidationIssue(BaseModel):
    code: str
    severity: Severity
    line_no: int
    field_path: str | None = None
    message: str
    suggestion: str
    kb_ref: str | None = None


class SimilarPart(BaseModel):
    part_id: int
    part_number: str
    title: str
    family: str
    score: float
    score_breakdown: dict[str, float]
    material_code: str | None
    ref_unit_cost_eur: float | None = None
    ref_qty: int | None = None
    ref_plant: str | None = None


class BOMLine(BaseModel):
    item_no: int
    part_number: str | None
    description: str
    qty_per: float
    unit: str = "pc"
    make_or_buy: MakeOrBuy
    source: Literal["catalog", "internal", "raw_material", "service", "new"]
    matched_ref: str | None = None
    match_method: Literal["exact", "normalized", "rule", "none"]
    unit_cost_eur: float | None = None
    confidence: float
    note: str | None = None


class RoutingOp(BaseModel):
    seq: int
    op_code: str
    work_center: str
    description: str
    setup_min: float
    cycle_min: float
    outsourced: bool = False
    service_code: str | None = None
    basis: Literal["rule", "blend", "manual"]
    rule_ids: list[str]
    ref_part_id: int | None = None
    blend_weight: float | None = None
    rule_cycle_min: float | None = None
    suggested: bool = False
    confidence: float


class CostLine(BaseModel):
    category: Literal[
        "material",
        "machining",
        "outsourced",
        "purchased",
        "internal_parts",
        "overhead",
        "logistics",
        "margin",
    ]
    description: str
    amount_eur_per_pc: float
    formula: str
    source: str


class CostBreakdown(BaseModel):
    plant: Plant
    qty: int
    feasible: bool
    infeasible_reason: str | None = None
    lines: list[CostLine] = []
    unit_cost_eur: float = 0.0
    unit_price_eur: float = 0.0
    total_price_eur: float = 0.0
    min_order_applied: bool = False
    lead_time_weeks: int = 0
    meets_due_date: bool | None = None


class ConfidenceReport(BaseModel):
    extraction: float
    similarity: float
    coverage: float
    price_sanity: float
    bom_match: float | None = None
    overall: float
    tier: Tier
    reasons: list[str]
