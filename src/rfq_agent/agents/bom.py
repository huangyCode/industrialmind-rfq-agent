"""BOM agent: raw stock + service lines for single parts, parts-list matching for assemblies (DESIGN §5.6)."""

from __future__ import annotations

from rfq_agent.agents.geometry import finished_volume_cm3, select_stock, weight_kg
from rfq_agent.config import BIZ
from rfq_agent.data.repositories import CatalogRepo, Material, PartRepo
from rfq_agent.models import BOMLine, DrawingSpec, MakeOrBuy, RoutingOp, ShapeClass

DEFAULT_REF_QTY = 200  # batch used to pick the closest historical cost of an internal part


def _single_part(spec: DrawingSpec, material: Material | None, routing: list[RoutingOp]) -> list[BOMLine]:
    stock = select_stock(spec, material.name if material else None)
    lines: list[BOMLine] = []
    if material is not None:
        lines.append(
            BOMLine(
                item_no=1,
                part_number=material.code,
                description=stock.description,
                qty_per=round(weight_kg(stock.volume_cm3, material.density_g_cm3), 3),
                unit="kg",
                make_or_buy=MakeOrBuy.BUY,
                source="raw_material",
                matched_ref=f"materials:{material.code}",
                match_method="rule",
                unit_cost_eur=material.price_eur_kg,
                confidence=0.9,
                note=f"stock volume {stock.volume_cm3:.1f} cm³ × {material.density_g_cm3} g/cm³",
            )
        )
    else:
        lines.append(
            BOMLine(
                item_no=1,
                part_number=None,
                description=stock.description + " — material unknown",
                qty_per=0.0,
                unit="kg",
                make_or_buy=MakeOrBuy.BUY,
                source="raw_material",
                matched_ref=None,
                match_method="none",
                unit_cost_eur=None,
                confidence=0.2,
                note=f"stock volume {stock.volume_cm3:.1f} cm³; material not resolved, weight and price open",
            )
        )
    fin_kg = weight_kg(finished_volume_cm3(spec), material.density_g_cm3) if material else None
    seen: set[str] = set()
    for op in routing:
        if not op.outsourced or not op.service_code or op.suggested or op.service_code in seen:
            continue
        seen.add(op.service_code)
        lines.append(
            BOMLine(
                item_no=len(lines) + 1,
                part_number=op.service_code,
                description=op.description,
                qty_per=round(fin_kg, 3) if fin_kg is not None else 0.0,
                unit="kg",
                make_or_buy=MakeOrBuy.SERVICE,
                source="service",
                matched_ref=f"outsourced_services:{op.service_code}",
                match_method="rule",
                unit_cost_eur=None,
                confidence=op.confidence,
                note=f"informational; priced in costing from routing op {op.seq}",
            )
        )
    return lines


def _assembly(spec: DrawingSpec, catalog: CatalogRepo, parts: PartRepo, ref_qty: int) -> list[BOMLine]:
    lines: list[BOMLine] = []
    for it in spec.parts_list:
        base = dict(
            item_no=it.item_no,
            part_number=it.part_number,
            description=it.description,
            qty_per=float(it.quantity),
        )
        # exact catalog number → internal part number → normalized catalog text; exact matches first so a
        # short catalog token set (e.g. DIN 471 "20") cannot shadow an internal part number
        hit = catalog.match(it.part_number, it.description, it.standard)
        internal = parts.by_number(it.part_number) if it.part_number else None
        if hit is not None and (hit[1] == "exact" or internal is None):
            row, how = hit
            lines.append(
                BOMLine(
                    **base,
                    make_or_buy=MakeOrBuy.BUY,
                    source="catalog",
                    matched_ref=row["part_number"],
                    match_method=how,
                    unit_cost_eur=row["unit_price_eur"],
                    confidence=0.95 if how == "exact" else 0.85,
                    note=f"{row['description']} ({row['supplier']}, {row['lead_time_days']} d)",
                )
            )
            continue
        if internal is not None:
            # Price make-parts at the assembly plant so the assembly cost is not a mix of plants
            # (quoting_policy.md#assemblies); fall back to another plant only with a lower confidence.
            cost = parts.closest_cost(internal["part_id"], ref_qty, plant=BIZ.assembly_plant)
            same_plant = cost is not None and cost["plant"] == BIZ.assembly_plant
            lines.append(
                BOMLine(
                    **base,
                    make_or_buy=MakeOrBuy.MAKE,
                    source="internal",
                    matched_ref=internal["part_number"],
                    match_method="exact",
                    unit_cost_eur=cost["unit_cost_eur"] if cost else None,
                    confidence=0.85 if same_plant else (0.6 if cost else 0.5),
                    note=(
                        f"historical cost {cost['plant']} @ qty {cost['qty']}"
                        + ("" if same_plant else f" (no {BIZ.assembly_plant} cost - engineering review)")
                        if cost
                        else "internal part without cost history"
                    ),
                )
            )
            continue
        lines.append(
            BOMLine(
                **base,
                make_or_buy=MakeOrBuy.MAKE,
                source="new",
                matched_ref=None,
                match_method="none",
                unit_cost_eur=None,
                confidence=0.2,
                note="no catalog or part-master match — drawing needed, unpriced (VAL-011)",
            )
        )
    return lines


def build_bom(
    spec: DrawingSpec,
    material: Material | None,
    routing: list[RoutingOp],
    catalog: CatalogRepo,
    parts: PartRepo,
    *,
    ref_qty: int = DEFAULT_REF_QTY,
) -> list[BOMLine]:
    """Single part → raw-material + service lines; assembly → parts list matched catalog → internal → new."""
    if spec.envelope.shape_class == ShapeClass.ASSEMBLY:
        return _assembly(spec, catalog, parts, ref_qty)
    return _single_part(spec, material, routing)
