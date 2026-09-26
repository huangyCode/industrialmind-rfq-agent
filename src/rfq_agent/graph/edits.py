"""Applying reviewer edits (DESIGN §5.10) and the before/after snapshots written as feedback."""

from __future__ import annotations

import copy

from rfq_agent.config import BIZ
from rfq_agent.models import ReviewEdit, RoutingOp

ROUTING_FIELDS = ("cycle_min", "setup_min", "add_op", "remove_op", "accept_suggested")


class EditError(ValueError):
    pass


def _norm_margin(v) -> float:
    m = float(v)
    if m > 1:  # "20" means 20 %
        m /= 100
    if not 0 <= m < 0.9:
        raise EditError(f"Margin {v!r} out of range")
    return m


def _find(routing: list[dict], seq: int | None, line_no: int) -> dict:
    for op in routing:
        if op["seq"] == seq:
            return op
    raise EditError(f"Line {line_no}: no operation with seq {seq}")


def apply_edits(
    lines: list[dict], margin_pct: float, plant_override: list[dict], edits: list[ReviewEdit]
) -> tuple[list[dict], float, list[dict]]:
    """Return edited copies of (lines, margin_pct, plant_override). Raises EditError on invalid edits."""
    lines = copy.deepcopy(lines)
    overrides = {o["line_no"]: o["plant"] for o in plant_override}
    by_no = {ln["line_no"]: ln for ln in lines}
    for e in edits:
        if e.target == "margin":
            margin_pct = _norm_margin(e.new_value)
            continue
        line = by_no.get(e.line_no)
        if line is None:
            raise EditError(f"No line {e.line_no}")
        if e.target == "plant":
            plant = str(e.new_value or "").upper()
            if not plant or plant in ("AUTO", "NONE"):
                overrides.pop(e.line_no, None)
                continue
            if plant not in BIZ.plants:
                raise EditError(f"Unknown plant {e.new_value!r}; choose one of {BIZ.plants}")
            feasible = {c["plant"] for c in line.get("costs", []) if c["feasible"]}
            if line.get("costs") and plant not in feasible:
                raise EditError(f"Line {e.line_no}: plant {plant} is not feasible for this routing")
            overrides[e.line_no] = plant
            continue
        # routing
        routing: list[dict] = line["routing"]
        if e.field in ("cycle_min", "setup_min"):
            op = _find(routing, e.seq, e.line_no)
            value = float(e.new_value)
            if value < 0:
                raise EditError(f"{e.field} must be ≥ 0")
            op[e.field] = value
            op["basis"] = "manual"
        elif e.field == "remove_op":
            op = _find(routing, e.seq, e.line_no)
            routing.remove(op)
        elif e.field == "accept_suggested":
            op = _find(routing, e.seq, e.line_no)
            if not op.get("suggested"):
                raise EditError(f"Line {e.line_no}: op {e.seq} is not a suggested operation")
            op["suggested"] = False
        elif e.field == "add_op":
            spec = dict(e.new_value or {})
            if not spec.get("op_code") or not spec.get("work_center"):
                raise EditError("add_op needs at least op_code and work_center")
            seq = int(spec.get("seq") or (max((o["seq"] for o in routing), default=0) + 5))
            if any(o["seq"] == seq for o in routing):
                raise EditError(f"Line {e.line_no}: seq {seq} already used")
            new = RoutingOp(
                seq=seq,
                op_code=spec["op_code"],
                work_center=spec["work_center"],
                description=spec.get("description") or f"{spec['op_code']} (added by reviewer)",
                setup_min=float(spec.get("setup_min") or 0),
                cycle_min=float(spec.get("cycle_min") or 0),
                outsourced=bool(spec.get("outsourced", False)),
                service_code=spec.get("service_code"),
                basis="manual",
                rule_ids=["manual"],
                confidence=1.0,
            )
            routing.append(new.model_dump(mode="json"))
            routing.sort(key=lambda o: o["seq"])
        else:
            raise EditError(f"Unknown routing edit field {e.field!r}; expected one of {ROUTING_FIELDS}")
    return lines, margin_pct, [{"line_no": k, "plant": v} for k, v in sorted(overrides.items())]


def snapshot(line: dict, margin_pct: float) -> dict:
    """What the reviewer changed and what it did to the price (per line)."""
    plant = line.get("recommended_plant")
    return {
        "routing": [
            {
                k: op.get(k)
                for k in ("seq", "op_code", "work_center", "setup_min", "cycle_min", "basis", "suggested")
            }
            | {"rule_cycle_min": op.get("rule_cycle_min")}
            for op in line.get("routing", [])
        ],
        "margin_pct": margin_pct,
        "plant": plant,
        "unit_prices": {
            f"{c['plant']}@{c['qty']}": round(c["unit_price_eur"], 2)
            for c in line.get("costs", [])
            if c["feasible"]
        },
        "tier": (line.get("confidence") or {}).get("tier"),
    }


def diff(before: dict, after: dict) -> list[dict]:
    """Field-level differences between two snapshots."""
    out: list[dict] = []
    b_ops = {o["seq"]: o for o in before["routing"]}
    a_ops = {o["seq"]: o for o in after["routing"]}
    for seq in sorted(b_ops.keys() | a_ops.keys()):
        b, a = b_ops.get(seq), a_ops.get(seq)
        if b is None:
            out.append({"path": f"routing[seq={seq}]", "before": None, "after": a["op_code"]})
        elif a is None:
            out.append({"path": f"routing[seq={seq}]", "before": b["op_code"], "after": None})
        else:
            for k in ("setup_min", "cycle_min", "suggested", "basis", "work_center"):
                if b.get(k) != a.get(k):
                    out.append(
                        {
                            "path": f"routing[seq={seq},{b['op_code']}].{k}",
                            "before": b.get(k),
                            "after": a.get(k),
                        }
                    )
    for k in ("margin_pct", "plant", "tier"):
        if before.get(k) != after.get(k):
            out.append({"path": k, "before": before.get(k), "after": after.get(k)})
    for k in sorted(before["unit_prices"].keys() | after["unit_prices"].keys()):
        b, a = before["unit_prices"].get(k), after["unit_prices"].get(k)
        if b != a:
            out.append({"path": f"unit_price[{k}]", "before": b, "after": a})
    return out
