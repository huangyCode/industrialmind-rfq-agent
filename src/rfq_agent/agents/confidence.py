"""Confidence scoring and triage (DESIGN §5.9).

Weights are empirical starting values; production calibrates them on reviewer edit rates so that
FAST_TRACK lines see < 5 % edits.
"""

from __future__ import annotations

from rfq_agent.config import BIZ
from rfq_agent.models import (
    BOMLine,
    ConfidenceReport,
    DrawingSpec,
    RFQItem,
    RoutingOp,
    Severity,
    ShapeClass,
    SimilarPart,
    Tier,
    ValidationIssue,
)

W_PART = {"extraction": 0.35, "similarity": 0.25, "coverage": 0.20, "price_sanity": 0.20}
W_ASSEMBLY = {
    "extraction": 0.30,
    "similarity": 0.20,
    "coverage": 0.15,
    "price_sanity": 0.15,
    "bom_match": 0.20,
}
NO_SIMILAR_SCORE = 0.3
PRICE_OK = 0.15  # |deviation| up to here counts as fully plausible
WEAK_SIMILAR = 0.8  # below this the similarity deduction gets an explicit reason
LOW_OP_CONFIDENCE = 0.6

_TITLE_FIELDS = (
    "part_number",
    "revision",
    "title",
    "material",
    "general_tolerance",
    "default_ra_um",
    "scale",
)


def _envelope_ok(spec: DrawingSpec) -> bool:
    e = spec.envelope
    if e.shape_class == ShapeClass.ROTATIONAL:
        return bool(e.max_diameter_mm and e.length_mm)
    if e.shape_class == ShapeClass.PRISMATIC:
        return bool(e.length_mm and e.width_mm and e.height_mm)
    return bool(e.length_mm or e.max_diameter_mm or e.width_mm or e.height_mm)


def _extraction(spec: DrawingSpec, item: RFQItem, reasons: list[str]) -> float:
    tb = spec.title_block
    is_asm = spec.envelope.shape_class == ShapeClass.ASSEMBLY
    required = {
        "part_number": bool(tb.part_number),
        "material": bool(tb.material) or is_asm,  # assemblies carry material per parts-list row
        "envelope": _envelope_ok(spec),
        "features": bool(spec.features) or (is_asm and bool(spec.parts_list)),
        "quantity": bool(item.quantities),
    }
    missing = [k for k, ok in required.items() if not ok]
    completeness = 1 - len(missing) / len(required)
    for k in missing:
        reasons.append(f"Required field '{k}' missing from drawing/RFQ")

    filled = [f for f in _TITLE_FIELDS if getattr(tb, f) not in (None, "")]
    no_ev = [f for f in filled if f not in tb.evidence]
    feats_no_ev = [f.id for f in spec.features if f.evidence is None]
    n_filled = len(filled) + len(spec.features)
    evidence = (n_filled - len(no_ev) - len(feats_no_ev)) / n_filled if n_filled else 0.0
    if no_ev:
        reasons.append(f"Title block field(s) without drawing evidence: {', '.join(no_ev)}")
    if feats_no_ev:
        reasons.append(f"Feature(s) without drawing evidence: {', '.join(feats_no_ev)}")
    if not n_filled:
        reasons.append("Nothing extracted with evidence from the drawing")

    n_incons = len(spec.extraction_warnings)
    for w in spec.extraction_warnings:
        reasons.append(f"Extraction consistency check failed: {w}")
    return min(1.0, max(0.0, completeness * evidence * (1 - 0.1 * n_incons)))


def _similarity(similar: list[SimilarPart], reasons: list[str]) -> float:
    if not similar:
        reasons.append(f"No similar historical part found (similarity set to {NO_SIMILAR_SCORE})")
        return NO_SIMILAR_SCORE
    top = max(similar, key=lambda s: s.score)
    if top.score < WEAK_SIMILAR:
        reasons.append(f"Closest historical part {top.part_number} only scores {top.score:.2f}")
    return top.score


def _coverage(spec: DrawingSpec, uncovered: list[str], reasons: list[str]) -> float:
    total = len(spec.features)
    if not total:
        return 1.0
    ids = set(uncovered)
    if ids:
        desc = [f"{f.id} ({f.description})" for f in spec.features if f.id in ids]
        reasons.append(f"{len(ids)}/{total} feature(s) not covered by any routing rule: {', '.join(desc)}")
    return max(0.0, 1 - len(ids) / total)


def _price_sanity(dev: float | None, reasons: list[str]) -> float:
    if dev is None:
        reasons.append("No comparable historical price - price plausibility unchecked")
        return 0.6
    if abs(dev) <= PRICE_OK:
        return 1.0
    if abs(dev) <= BIZ.price_deviation_flag:
        reasons.append(f"Price deviates {dev:+.0%} from scaled reference part (> ±{PRICE_OK:.0%})")
        return 0.7
    reasons.append(
        f"Price deviates {dev:+.0%} from scaled reference part (> ±{BIZ.price_deviation_flag:.0%}, flagged)"
    )
    return 0.4


def _bom_match(bom: list[BOMLine], reasons: list[str]) -> float:
    lines = [b for b in bom if b.source != "service"]
    if not lines:
        reasons.append("Assembly has no BOM lines")
        return 0.0
    unpriced = [b for b in lines if b.unit_cost_eur is None]
    for b in unpriced:
        reasons.append(f"BOM item {b.item_no} {b.part_number or b.description} unpriced (source={b.source})")
    return (len(lines) - len(unpriced)) / len(lines)


def _routing_notes(routing: list[RoutingOp], similar: list[SimilarPart], reasons: list[str]) -> None:
    """Highlights for the reviewer; not part of the weighted score."""
    calibrated = bool(similar) and max(s.score for s in similar) >= BIZ.blend_min_score
    numbers = {s.part_id: s.part_number for s in similar}
    for op in routing:
        if op.suggested:
            ref = numbers.get(op.ref_part_id) if op.ref_part_id is not None else None
            src = f"reference part {ref}" if ref else "a reference part"
            reasons.append(f"Suggested op {op.op_code} from {src} (not costed)")
        elif op.confidence < LOW_OP_CONFIDENCE:
            reasons.append(f"Op {op.seq} {op.work_center} low confidence {op.confidence:.2f}")
        elif op.basis == "rule" and not op.outsourced and calibrated:
            reasons.append(f"Op {op.seq} {op.work_center} rule-based only, no historical reference")


def assess(
    *,
    spec: DrawingSpec,
    item: RFQItem,
    issues: list[ValidationIssue],
    similar: list[SimilarPart],
    routing: list[RoutingOp],
    uncovered: list[str],
    bom: list[BOMLine],
    price_deviation_pct: float | None,
) -> ConfidenceReport:
    """Weighted confidence + tier. `issues` are this line's validation issues."""
    reasons: list[str] = []
    blockers = [i for i in issues if i.severity == Severity.BLOCKER]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    for i in blockers:
        reasons.append(f"BLOCKER {i.code}: {i.message}")
    for i in warnings:
        reasons.append(f"WARNING {i.code}: {i.message}")

    scores = {
        "extraction": _extraction(spec, item, reasons),
        "similarity": _similarity(similar, reasons),
        "coverage": _coverage(spec, uncovered, reasons),
        "price_sanity": _price_sanity(price_deviation_pct, reasons),
    }
    is_asm = spec.envelope.shape_class == ShapeClass.ASSEMBLY
    if is_asm:
        scores["bom_match"] = _bom_match(bom, reasons)
    weights = W_ASSEMBLY if is_asm else W_PART
    overall = sum(w * scores[k] for k, w in weights.items())
    _routing_notes(routing, similar, reasons)

    if blockers:
        overall = 0.0
        tier = Tier.MANUAL
    elif overall < BIZ.manual_threshold:
        tier = Tier.MANUAL
        reasons.append(f"Overall confidence {overall:.2f} below manual threshold {BIZ.manual_threshold:.2f}")
    else:
        # One-click approval needs a high score AND a close precedent AND a plausible price;
        # otherwise the engineer must look at the highlighted items.
        top = similar[0].score if similar else 0.0
        gates = []
        if warnings:
            gates.append("warnings present")
        if top < BIZ.fast_track_min_similarity:
            gates.append(
                f"no close precedent (top similarity {top:.2f} < {BIZ.fast_track_min_similarity:.2f})"
            )
        if price_deviation_pct is None or abs(price_deviation_pct) > BIZ.price_deviation_flag:
            gates.append("price not confirmed by a reference part")
        if overall >= BIZ.fast_track_threshold and not gates:
            tier = Tier.FAST_TRACK
        else:
            tier = Tier.STANDARD
            if overall >= BIZ.fast_track_threshold:
                reasons.append("Capped at STANDARD: " + "; ".join(gates))

    return ConfidenceReport(
        extraction=scores["extraction"],
        similarity=scores["similarity"],
        coverage=scores["coverage"],
        price_sanity=scores["price_sanity"],
        bom_match=scores.get("bom_match"),
        overall=overall,
        tier=tier,
        reasons=reasons,
    )
