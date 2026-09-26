"""Deterministic should-cost engine (DESIGN §5.8).

Every number that reaches a quote is computed here, never by an LLM. Each CostLine carries the
formula with substituted values and the master-data source it came from.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

from rfq_agent.config import BIZ
from rfq_agent.data.repositories import PartRepo, RateRepo
from rfq_agent.models import BOMLine, CostBreakdown, CostLine, MakeOrBuy, RoutingOp, SimilarPart


def _m(x: float) -> str:
    """Money / rate for display in formula strings."""
    return f"{x:.2f}"


def _n(x: float) -> str:
    """Compact number (setup minutes, quantities)."""
    return f"{x:g}"


def _material_lines(bom: list[BOMLine]) -> list[CostLine]:
    out = []
    for b in bom:
        if b.source != "raw_material":
            continue
        ref = b.matched_ref or b.part_number or b.description
        if b.unit_cost_eur is None:
            out.append(
                CostLine(
                    category="material",
                    description=f"{b.description}: material price unknown - price on request",
                    amount_eur_per_pc=0.0,
                    formula="not priced",
                    source=f"materials:{ref}",
                )
            )
            continue
        amount = b.qty_per * b.unit_cost_eur * (1 + BIZ.scrap_pct)
        out.append(
            CostLine(
                category="material",
                description=b.description,
                amount_eur_per_pc=amount,
                formula=f"{b.qty_per:.3f} kg × {_m(b.unit_cost_eur)} €/kg × (1 + {_n(BIZ.scrap_pct)})",
                source=f"materials:{ref}",
            )
        )
    return out


_HIST_PLANT = re.compile(r"historical cost (\w+) @")


def internal_cost_plant(b: BOMLine) -> str | None:
    """Plant whose historical cost prices an internal make part (from the BOM note), None if unknown."""
    m = _HIST_PLANT.search(b.note or "") if b.source == "internal" else None
    return m.group(1) if m and m.group(1) in BIZ.plants else None


def _bom_part_lines(bom: list[BOMLine]) -> list[CostLine]:
    """Purchased (catalog), internal make parts and unpriced new parts of an assembly."""
    out = []
    for b in bom:
        label = f"Item {b.item_no} {b.part_number or ''} {b.description}".replace("  ", " ")
        if b.source in ("catalog", "internal") and b.unit_cost_eur is not None:
            cat = "purchased" if b.source == "catalog" else "internal_parts"
            src = f"catalog:{b.matched_ref or b.part_number}"
            if b.source == "internal":
                # `parts:<number>@<plant>` records where the make part's cost comes from (see recommend_plant)
                plant = internal_cost_plant(b)
                src = f"parts:{b.matched_ref or b.part_number}" + (f"@{plant}" if plant else "")
            out.append(
                CostLine(
                    category=cat,
                    description=label,
                    amount_eur_per_pc=b.qty_per * b.unit_cost_eur,
                    formula=f"{_n(b.qty_per)} {b.unit} × {_m(b.unit_cost_eur)} €",
                    source=src,
                )
            )
        elif b.source in ("catalog", "internal", "new"):
            # Keep the gap visible instead of silently pricing it at zero.
            cat = "internal_parts" if b.make_or_buy == MakeOrBuy.MAKE else "purchased"
            out.append(
                CostLine(
                    category=cat,
                    description=f"{label}: not in catalog / master data - price on request",
                    amount_eur_per_pc=0.0,
                    formula=f"{_n(b.qty_per)} {b.unit} × ? € (unpriced)",
                    source=f"bom:{b.source}",
                )
            )
    return out


def _machining_line(op: RoutingOp, plant: str, qty: int, rate: float) -> CostLine:
    amount = (op.setup_min / qty + op.cycle_min) / 60 * rate
    basis = op.basis
    if op.basis == "blend" and op.blend_weight is not None:
        basis = f"blend w={op.blend_weight:.2f} ref part {op.ref_part_id}"
    rules = ",".join(op.rule_ids) or "-"
    return CostLine(
        category="machining",
        description=f"Op {op.seq} {op.description} ({op.work_center}; {rules}; {basis})",
        amount_eur_per_pc=amount,
        formula=f"({_n(op.setup_min)}/{qty} + {op.cycle_min:.2f}) min / 60 × {_m(rate)} €/h",
        source=f"rates:{plant}/{op.work_center}",
    )


def _outsourced_line(op: RoutingOp, qty: int, weight_kg: float, services: dict) -> CostLine:
    svc = services.get(op.service_code or "")
    if svc is None:
        return CostLine(
            category="outsourced",
            description=f"Op {op.seq} {op.description}: unknown service '{op.service_code}' - price on request",
            amount_eur_per_pc=0.0,
            formula="not priced",
            source=f"services:{op.service_code}",
        )
    by_weight = svc["price_eur_kg"] * weight_kg
    lot_share = svc["lot_min_eur"] / qty
    return CostLine(
        category="outsourced",
        description=f"Op {op.seq} {svc['name']} ({op.service_code})",
        amount_eur_per_pc=max(by_weight, lot_share),
        formula=(
            f"max({_m(svc['price_eur_kg'])} €/kg × {weight_kg:.3f} kg, "
            f"{_m(svc['lot_min_eur'])} € / {qty}) = max({_m(by_weight)}, {_m(lot_share)})"
        ),
        source=f"services:{op.service_code}",
    )


def _one(
    *,
    plant: str,
    qty: int,
    ops: list[RoutingOp],
    bom: list[BOMLine],
    weight_kg: float,
    requested_delivery: date | None,
    today: date,
    rates: RateRepo,
    services: dict,
    margin_pct: float,
) -> CostBreakdown:
    pinfo = rates.plants.get(plant)
    has_outsourced = any(op.outsourced for op in ops)
    lead = 0
    meets = None
    if pinfo:
        lead = int(pinfo["base_lead_time_weeks"]) + (BIZ.outsourced_extra_weeks if has_outsourced else 0)
        if requested_delivery is not None:
            meets = today + timedelta(weeks=lead) <= requested_delivery

    missing = [
        op.work_center for op in ops if not op.outsourced and rates.rate(plant, op.work_center) is None
    ]
    reason = None
    if pinfo is None:
        reason = f"Plant {plant} missing from plant master data"
    elif missing:
        wcs = ", ".join(dict.fromkeys(missing))
        reason = f"Work center {wcs} not available at plant {plant}"
    if reason:
        return CostBreakdown(
            plant=plant,
            qty=qty,
            feasible=False,
            infeasible_reason=reason,
            lead_time_weeks=lead,
            meets_due_date=meets,
        )

    lines = _material_lines(bom)
    for op in ops:
        if op.outsourced:
            lines.append(_outsourced_line(op, qty, weight_kg, services))
        else:
            lines.append(_machining_line(op, plant, qty, rates.rate(plant, op.work_center)))
    lines += _bom_part_lines(bom)

    subtotal = sum(line.amount_eur_per_pc for line in lines)
    oh_pct, log_pct = pinfo["overhead_pct"], pinfo["logistics_pct"]
    overhead = subtotal * oh_pct
    logistics = subtotal * log_pct
    unit_cost = subtotal + overhead + logistics
    unit_price = unit_cost / (1 - margin_pct)
    lines += [
        CostLine(
            category="overhead",
            description=f"Overhead {plant} ({oh_pct:.0%} of direct cost)",
            amount_eur_per_pc=overhead,
            formula=f"{_m(subtotal)} € × {_n(oh_pct)}",
            source=f"plants:{plant}",
        ),
        CostLine(
            category="logistics",
            description=f"Logistics & duties {plant} → DE customer ({log_pct:.0%})",
            amount_eur_per_pc=logistics,
            formula=f"{_m(subtotal)} € × {_n(log_pct)}",
            source=f"plants:{plant}",
        ),
        CostLine(
            category="margin",
            description=f"Target margin {margin_pct:.0%} on price",
            amount_eur_per_pc=unit_price - unit_cost,
            formula=f"{_m(unit_cost)} € / (1 − {_n(margin_pct)}) − {_m(unit_cost)} €",
            source="policy:margin_pct",
        ),
    ]
    total = unit_price * qty
    floor = total < BIZ.min_order_value_eur
    return CostBreakdown(
        plant=plant,
        qty=qty,
        feasible=True,
        lines=lines,
        unit_cost_eur=unit_cost,
        unit_price_eur=unit_price,
        total_price_eur=BIZ.min_order_value_eur if floor else total,
        min_order_applied=floor,
        lead_time_weeks=lead,
        meets_due_date=meets,
    )


def compute_costs(
    *,
    routing: list[RoutingOp],
    bom: list[BOMLine],
    quantities: list[int],
    finished_weight_kg: float,
    requested_delivery: date | None,
    today: date,
    rates: RateRepo,
    services: dict,
    margin_pct: float = BIZ.margin_pct,
) -> list[CostBreakdown]:
    """One CostBreakdown per (plant × quantity). Suggested ops are excluded from cost and feasibility.

    Amounts keep full float precision; only the formula strings are rounded for display.
    """
    ops = [op for op in routing if not op.suggested]
    return [
        _one(
            plant=plant,
            qty=qty,
            ops=ops,
            bom=bom,
            weight_kg=finished_weight_kg,
            requested_delivery=requested_delivery,
            today=today,
            rates=rates,
            services=services,
            margin_pct=margin_pct,
        )
        for plant in BIZ.plants
        for qty in quantities
        if qty > 0
    ]


def make_part_plant(costs: list[CostBreakdown]) -> str | None:
    """Plant where an assembly's make parts come from; None when the line has no make parts.

    quoting_policy.md#assemblies: an assembly is built where its make parts are produced. Internal parts
    carry the plant of their historical cost (`parts:<number>@<plant>`). One common plant → that plant;
    mixed plants, a make part without a known plant, or an unpriced new make part → DE (home plant, full
    capability; mixed-plant supply needs engineering review anyway).
    """
    make = [ln for c in costs if c.feasible for ln in c.lines if ln.category == "internal_parts"]
    if not make:
        return None
    plants = {ln.source.rpartition("@")[2] if "@" in ln.source else None for ln in make}
    return plants.pop() if len(plants) == 1 and None not in plants else "DE"


def recommend_plant(costs: list[CostBreakdown], qty: int) -> str | None:
    """Recommended plant for the primary quantity (the price table still lists every plant).

    Assemblies with make parts: the make-part plant (`make_part_plant`) if it is feasible for the assembly
    routing. Otherwise the cheapest feasible plant meeting the due date (no due date counts as met); else
    the cheapest feasible. Prices equal to the cent are a tie, resolved in favour of DE, then BIZ.plants
    order. A reviewer's plant override takes precedence over this (graph `plant_override`).
    """
    feasible = [c for c in costs if c.qty == qty and c.feasible]
    if not feasible:
        return None
    home = make_part_plant(feasible)
    if home and any(c.plant == home for c in feasible):
        return home
    on_time = [c for c in feasible if c.meets_due_date is not False]
    pool = on_time or feasible
    order = {p: i for i, p in enumerate(BIZ.plants)}
    best = min(pool, key=lambda c: (round(c.unit_price_eur, 2), c.plant != "DE", order.get(c.plant, 99)))
    return best.plant


def _env_volume_cm3(row: dict) -> float | None:
    d, length = row.get("max_diameter_mm"), row.get("length_mm")
    w, h = row.get("width_mm"), row.get("height_mm")
    if d and length:
        return math.pi / 4 * d * d * length / 1000
    if length and w and h:
        return length * w * h / 1000
    return None


def price_deviation(
    costs: list[CostBreakdown],
    recommended_plant: str | None,
    qty: int,
    similar: list[SimilarPart],
    new_env_volume_cm3: float,
    part_repo: PartRepo,
) -> float | None:
    """Signed deviation of our figure vs the top similar part at the same plant and nearest qty.

    Reference scaled by (V_new / V_ref)^(2/3). Prefers historical actual cost (compared with our unit
    cost); falls back to historical quote price (compared with our unit price). None if no reference.
    """
    if not recommended_plant or not similar or new_env_volume_cm3 <= 0:
        return None
    ours = next((c for c in costs if c.plant == recommended_plant and c.qty == qty and c.feasible), None)
    if ours is None:
        return None
    top = max(similar, key=lambda s: s.score)
    try:
        ref_vol = _env_volume_cm3(part_repo.get(top.part_id))
    except IndexError:
        return None
    if not ref_vol:
        return None
    scale = (new_env_volume_cm3 / ref_vol) ** (2 / 3)

    same_plant_costs = [r for r in part_repo.costs(top.part_id) if r["plant"] == recommended_plant]
    if same_plant_costs:
        ref = min(same_plant_costs, key=lambda r: (abs(r["qty"] - qty), r["unit_cost_eur"]))
        mine, ref_val = ours.unit_cost_eur, ref["unit_cost_eur"]
    else:
        quotes = [r for r in part_repo.quotes(top.part_id) if r["plant"] == recommended_plant]
        if not quotes:
            return None
        ref = min(quotes, key=lambda r: (abs(r["qty"] - qty), r["unit_price_eur"]))
        mine, ref_val = ours.unit_price_eur, ref["unit_price_eur"]
    scaled = ref_val * scale
    if scaled <= 0:
        return None
    return (mine - scaled) / scaled
