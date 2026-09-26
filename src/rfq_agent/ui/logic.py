"""Pure (Streamlit-free) helpers behind the workbench pages, so they can be unit-tested directly."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from rfq_agent.models import CostBreakdown, ReviewEdit, RoutingOp

# ---------- routing editor ----------

EDITOR_COLUMNS = [
    "seq",
    "op_code",
    "work_center",
    "setup_min",
    "cycle_min",
    "basis",
    "rule_cycle_min",
    "blend_weight",
    "status",
    "accept",
    "rules",
    "description",
]


def routing_frame(ops: list[RoutingOp]) -> pd.DataFrame:
    rows = []
    for op in ops:
        if op.suggested:
            status = "suggested (not costed)"
        elif op.outsourced:
            status = f"outsourced ({op.service_code})" if op.service_code else "outsourced"
        else:
            status = "costed"
        rows.append(
            {
                "seq": op.seq,
                "op_code": op.op_code,
                "work_center": op.work_center,
                "description": op.description,
                "setup_min": op.setup_min,
                "cycle_min": op.cycle_min,
                "basis": op.basis,
                "rule_cycle_min": op.rule_cycle_min,
                "blend_weight": op.blend_weight,
                "rules": ", ".join(op.rule_ids),
                "status": status,
                "accept": False,
            }
        )
    return pd.DataFrame(rows, columns=EDITOR_COLUMNS)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _str(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def routing_edits(line_no: int, ops: list[RoutingOp], edited: pd.DataFrame | list[dict]) -> list[ReviewEdit]:
    """Translate the edited routing table into ReviewEdits (cycle/setup, accept, remove, add)."""
    records = edited.to_dict("records") if isinstance(edited, pd.DataFrame) else list(edited)
    by_seq = {op.seq: op for op in ops}
    seen: set[int] = set()
    edits: list[ReviewEdit] = []
    added: list[dict] = []
    for r in records:
        seq = _num(r.get("seq"))
        op = by_seq.get(int(seq)) if seq is not None and int(seq) in by_seq and int(seq) not in seen else None
        if op is None:
            if not _str(r.get("op_code")):
                continue  # empty row the user started and abandoned
            added.append(r)
            continue
        seen.add(op.seq)
        for field in ("cycle_min", "setup_min"):
            v = _num(r.get(field))
            if v is not None and abs(v - getattr(op, field)) > 1e-9:
                edits.append(
                    ReviewEdit(line_no=line_no, target="routing", seq=op.seq, field=field, new_value=v)
                )
        if op.suggested and bool(r.get("accept")):
            edits.append(ReviewEdit(line_no=line_no, target="routing", seq=op.seq, field="accept_suggested"))
    for seq in by_seq.keys() - seen:
        edits.append(ReviewEdit(line_no=line_no, target="routing", seq=seq, field="remove_op"))
    for r in added:
        seq = _num(r.get("seq"))
        edits.append(
            ReviewEdit(
                line_no=line_no,
                target="routing",
                field="add_op",
                new_value={
                    "seq": int(seq) if seq is not None else None,
                    "op_code": _str(r.get("op_code")).upper(),
                    "work_center": (_str(r.get("work_center")) or _str(r.get("op_code"))).upper(),
                    "description": _str(r.get("description")) or None,
                    "setup_min": _num(r.get("setup_min")) or 0.0,
                    "cycle_min": _num(r.get("cycle_min")) or 0.0,
                },
            )
        )
    return edits


# ---------- costs ----------


def price_matrix(costs: list[CostBreakdown], recommended: str | None, plants: list[str]) -> pd.DataFrame:
    """One row per quantity, one column per plant: unit price or the infeasibility reason."""
    qtys = sorted({c.qty for c in costs})
    rows = []
    for q in qtys:
        row: dict[str, Any] = {"qty": q}
        for p in plants:
            c = next((c for c in costs if c.plant == p and c.qty == q), None)
            label = p + (" (recommended)" if p == recommended else "")
            if c is None:
                row[label] = "-"
            elif not c.feasible:
                row[label] = "not feasible"
            else:
                cell = f"{c.unit_price_eur:,.2f}"
                if c.min_order_applied:
                    cell += " (min. order)"
                if c.meets_due_date is False:
                    cell += " (late)"
                row[label] = cell
        rows.append(row)
    return pd.DataFrame(rows)


def cost_detail_frame(c: CostBreakdown) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "category": ln.category,
                "description": ln.description,
                "EUR / pc": round(ln.amount_eur_per_pc, 4),
                "formula": ln.formula,
                "source": ln.source,
            }
            for ln in c.lines
        ]
    )


def price_changes(diff: list[dict]) -> pd.DataFrame:
    """Rows of a feedback diff that concern prices, with the absolute and relative change."""
    rows = []
    for d in diff:
        path = d["path"]
        if not path.startswith("unit_price["):
            continue
        b, a = d.get("before"), d.get("after")
        rows.append(
            {
                "plant @ qty": path[len("unit_price[") : -1],
                "before EUR": b,
                "after EUR": a,
                "change EUR": round(a - b, 2) if a is not None and b is not None else None,
                "change %": round((a - b) / b * 100, 2) if a is not None and b else None,
            }
        )
    return pd.DataFrame(rows)


# ---------- metrics ----------

# README §7 business-case assumptions (engineer minutes per RFQ by triage tier).
MINUTES_PER_TIER = {"fast_track": 30, "standard": 90, "manual": 240}
BASELINE_MINUTES = 240
ENGINEER_EUR_PER_H = 65


def business_view(runs: list[dict]) -> dict:
    """Estimated engineer time for the processed runs (RFQ tier = worst line tier)."""
    counted = []
    for r in runs:
        tiers = list((r.get("tiers") or {}).values())
        if not tiers:
            continue
        tier = "manual" if "manual" in tiers else "standard" if "standard" in tiers else "fast_track"
        counted.append((r["rfq_id"], tier))
    by_tier = {t: sum(1 for _, x in counted if x == t) for t in MINUTES_PER_TIER}
    minutes = sum(MINUTES_PER_TIER[t] for _, t in counted)
    baseline = BASELINE_MINUTES * len(counted)
    saved = baseline - minutes
    return {
        "rfqs": len(counted),
        "by_tier": by_tier,
        "minutes": minutes,
        "baseline_minutes": baseline,
        "saved_minutes": saved,
        "saved_pct": (saved / baseline * 100) if baseline else 0.0,
        "saved_eur": saved / 60 * ENGINEER_EUR_PER_H,
        "not_counted": [r["rfq_id"] for r in runs if not r.get("tiers")],
    }


def trace_totals(trace: list) -> dict:
    return {
        "duration_s": sum(e.duration_ms for e in trace) / 1000,
        "llm_calls": sum(e.llm_calls for e in trace),
        "cache_hits": sum(e.cache_hits for e in trace),
        "input_tokens": sum(e.input_tokens for e in trace),
        "output_tokens": sum(e.output_tokens for e in trace),
    }


def trace_frame(trace: list) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "node": e.node,
                "started": e.started_at.strftime("%H:%M:%S"),
                "duration ms": e.duration_ms,
                "LLM calls": e.llm_calls,
                "cache hits": e.cache_hits,
                "tokens in/out": f"{e.input_tokens}/{e.output_tokens}",
                "summary": e.summary,
            }
            for e in trace
        ]
    )
