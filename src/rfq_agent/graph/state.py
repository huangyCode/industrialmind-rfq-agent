"""Graph state (DESIGN §6.2, adapted).

The state holds JSON-native values only (pydantic models are stored as `model_dump(mode="json")`), so the
SQLite checkpoint needs no custom (de)serialisers and survives code reloads. Per-line work products live
in `lines` (one dict per RFQ line, keys as in LineQuoteDraft) instead of one dict per artefact.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class RFQState(TypedDict, total=False):
    rfq_id: str
    rfq_dir: str
    extractor: str  # "llm" | "reference"
    status: str  # RFQStatus value
    request: dict  # RFQRequest
    lines: list[dict]  # per line: line_no, item, spec, render_paths, issues, similar_parts, bom, routing,
    #                    uncovered_features, quantities, costs, recommended_plant, price_deviation_pct, confidence
    issues: list[dict]  # all ValidationIssues of the RFQ
    margin_pct: float
    plant_override: list[dict]  # [{"line_no": 1, "plant": "PL"}]
    draft: dict | None  # QuoteDraft
    review: dict | None  # last ReviewDecision
    review_round: int
    review_error: str | None
    pending_feedback: dict | None  # edits applied, waiting for the recomputed "after" snapshot
    clarification: dict | None  # {"subject", "body", "questions", "language", "source"}
    clarification_email: str | None
    quote: dict | None  # Quote
    quote_paths: dict | None  # {"html": ..., "json": ...}
    reviews: Annotated[list[dict], operator.add]  # decision history
    trace: Annotated[list[dict], operator.add]  # TraceEvent, appended by every node
    error: str | None
    extra: dict[str, Any]
