"""Similar-part retrieval with an explainable weighted feature score (DESIGN §5.5, CONTRACTS §1.8).

The same feature vector is computed from a new `DrawingSpec` (`featurize_spec`) and from a row of the
historical `parts` table (`featurize_part`); the parts table is filled from `featurize_spec` of each
historical spec, so both paths give identical features for the same part.
"""

from __future__ import annotations

import math

from rfq_agent.agents.geometry import feature_it_grade, main_dims
from rfq_agent.config import BIZ
from rfq_agent.data.repositories import MaterialRepo, PartRepo
from rfq_agent.models import DrawingSpec, FeatureType, Severity, ShapeClass, SimilarPart, ValidationIssue

WEIGHTS = {
    "diameter": 0.20,
    "length": 0.15,
    "material": 0.15,
    "holes": 0.10,
    "threads": 0.10,
    "features": 0.15,
    "precision": 0.15,
}
SIZE_TAU = 0.35  # exp(-|ln a − ln b| / τ)
IT_TAU = 2.0  # exp(-|ΔIT| / τ)
SHAPE_PENALTY = 0.3  # different shape class: total × 0.3 (soft filter)
UNKNOWN_SIZE_SCORE = 0.5
DEFAULT_IT = 12  # no toleranced feature ≈ general tolerance ISO 2768-m
FLAGS = ("has_keyway", "has_gear", "has_heat_treatment", "has_surface_treatment")

REUSE_MIN_SCORE = 0.92
REUSE_MAX_DIM_DIFF = 0.05
REUSE_KB_REF = "quoting_policy.md#part-reuse"
DEFAULT_REF_QTY = 200


def _main_dim(shape: str, d: float | None, w: float | None, h: float | None) -> float:
    """Rotational: max OD; prismatic: max(width, height); assembly: OD if given, else max(width, height)."""
    if shape == ShapeClass.ROTATIONAL.value:
        return float(d or 0.0)
    if shape == ShapeClass.PRISMATIC.value:
        return float(max(w or 0.0, h or 0.0))
    return float(d or max(w or 0.0, h or 0.0))


def _group(code: str | None, materials: MaterialRepo) -> str | None:
    m = materials.all.get(code) if code else None
    return m.mat_group if m else None


def featurize_spec(spec: DrawingSpec, materials: MaterialRepo) -> dict:
    """Feature vector of a (post-processed) drawing spec."""
    shape = spec.envelope.shape_class.value
    d, length, w, h = main_dims(spec)
    tb = spec.title_block
    code = tb.material_code or materials.resolve(tb.material)
    its = [g for f in spec.features if (g := feature_it_grade(f.nominal_mm, f.tolerance)) is not None]
    ras = [f.ra_um for f in spec.features if f.ra_um is not None]

    def count(t: FeatureType) -> int:
        return sum(f.quantity for f in spec.features if f.type == t)

    return {
        "shape_class": shape,
        "main_dim_mm": round(_main_dim(shape, d, w, h), 3),
        "length_mm": round(float(length or 0.0), 3),
        "material_code": code,
        "mat_group": _group(code, materials),
        "n_holes": count(FeatureType.HOLE),
        "n_threads": count(FeatureType.THREAD),
        "has_keyway": count(FeatureType.KEYWAY) > 0,
        "has_gear": count(FeatureType.GEAR_TEETH) > 0,
        "has_heat_treatment": bool((spec.heat_treatment or "").strip()),
        "has_surface_treatment": bool((spec.surface_treatment or "").strip()),
        "min_it_grade": min(its) if its else None,
        "min_ra_um": min(ras) if ras else None,
    }


def part_columns(spec: DrawingSpec, materials: MaterialRepo) -> dict:
    """`parts` table columns derived from a spec (used by the history generator; keeps both paths equal)."""
    d, length, w, h = main_dims(spec)
    f = featurize_spec(spec, materials)
    return {
        "shape_class": f["shape_class"],
        "material_code": f["material_code"],
        "max_diameter_mm": round(d, 3) if d else None,
        "length_mm": f["length_mm"] or None,
        "width_mm": round(w, 3) if w else None,
        "height_mm": round(h, 3) if h else None,
        "n_holes": f["n_holes"],
        "n_threads": f["n_threads"],
        "has_keyway": int(f["has_keyway"]),
        "has_gear": int(f["has_gear"]),
        "heat_treatment": spec.heat_treatment,
        "surface_treatment": spec.surface_treatment,
        "min_it_grade": f["min_it_grade"],
        "min_ra_um": f["min_ra_um"],
    }


def featurize_part(row: dict, materials: MaterialRepo) -> dict:
    """Feature vector of a historical part (row of the `parts` table)."""
    shape = row.get("shape_class") or ShapeClass.ROTATIONAL.value
    code = row.get("material_code")
    return {
        "shape_class": shape,
        "main_dim_mm": round(
            _main_dim(shape, row.get("max_diameter_mm"), row.get("width_mm"), row.get("height_mm")), 3
        ),
        "length_mm": round(float(row.get("length_mm") or 0.0), 3),
        "material_code": code,
        "mat_group": _group(code, materials),
        "n_holes": int(row.get("n_holes") or 0),
        "n_threads": int(row.get("n_threads") or 0),
        "has_keyway": bool(row.get("has_keyway")),
        "has_gear": bool(row.get("has_gear")),
        "has_heat_treatment": bool((row.get("heat_treatment") or "").strip()),
        "has_surface_treatment": bool((row.get("surface_treatment") or "").strip()),
        "min_it_grade": row.get("min_it_grade"),
        "min_ra_um": row.get("min_ra_um"),
    }


# ---------- scoring ----------


def _size_sim(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return UNKNOWN_SIZE_SCORE
    return math.exp(-abs(math.log(a) - math.log(b)) / SIZE_TAU)


def _count_sim(a: int, b: int) -> float:
    return 1 - abs(a - b) / max(a, b, 1)


def _material_sim(a: dict, b: dict) -> float:
    if a["material_code"] is None and b["material_code"] is None:
        return 1.0  # e.g. two assemblies ("see parts list")
    if a["material_code"] and a["material_code"] == b["material_code"]:
        return 1.0
    if a["mat_group"] and a["mat_group"] == b["mat_group"]:
        return 0.7
    return 0.3


def score_features(new: dict, ref: dict) -> tuple[float, dict[str, float]]:
    """Weighted score in [0, 1] and per-component breakdown (plus the shape factor under 'shape')."""
    it_a = new["min_it_grade"] if new["min_it_grade"] is not None else DEFAULT_IT
    it_b = ref["min_it_grade"] if ref["min_it_grade"] is not None else DEFAULT_IT
    parts = {
        "diameter": _size_sim(new["main_dim_mm"], ref["main_dim_mm"]),
        "length": _size_sim(new["length_mm"], ref["length_mm"]),
        "material": _material_sim(new, ref),
        "holes": _count_sim(new["n_holes"], ref["n_holes"]),
        "threads": _count_sim(new["n_threads"], ref["n_threads"]),
        "features": sum(new[k] == ref[k] for k in FLAGS) / len(FLAGS),
        "precision": math.exp(-abs(it_a - it_b) / IT_TAU),
    }
    shape = 1.0 if new["shape_class"] == ref["shape_class"] else SHAPE_PENALTY
    score = shape * sum(WEIGHTS[k] * v for k, v in parts.items())
    breakdown = {k: round(v, 4) for k, v in parts.items()}
    breakdown["shape"] = shape
    return round(score, 4), breakdown


def _ref_fields(parts: PartRepo, part_id: int, qty: int) -> tuple[float | None, int | None, str | None]:
    cost = parts.closest_cost(part_id, qty)
    if cost:
        return cost["unit_cost_eur"], cost["qty"], cost["plant"]
    quotes = parts.quotes(part_id)
    if quotes:
        q = min(quotes, key=lambda r: (abs(r["qty"] - qty), r["unit_price_eur"]))
        return q["unit_price_eur"], q["qty"], q["plant"]
    return None, None, None


def find_similar(
    spec: DrawingSpec,
    parts: PartRepo,
    materials: MaterialRepo,
    exclude_part_id: int | None = None,
    top_k: int = BIZ.similar_top_k,
    *,
    qty: int = DEFAULT_REF_QTY,
) -> list[SimilarPart]:
    """Top-k historical parts with score ≥ BIZ.similar_min_score (best first).

    `exclude_part_id` supports leave-one-out backtesting; `qty` picks the reference cost batch.
    """
    new = featurize_spec(spec, materials)
    scored = []
    for row in parts.all():
        if exclude_part_id is not None and row["part_id"] == exclude_part_id:
            continue
        score, breakdown = score_features(new, featurize_part(row, materials))
        if score >= BIZ.similar_min_score:
            scored.append((score, row, breakdown))
    scored.sort(key=lambda t: (-t[0], t[1]["part_id"]))
    out = []
    for score, row, breakdown in scored[:top_k]:
        cost, ref_qty, plant = _ref_fields(parts, row["part_id"], qty)
        out.append(
            SimilarPart(
                part_id=row["part_id"],
                part_number=row["part_number"],
                title=row["title"] or "",
                family=row["family"] or "",
                score=score,
                score_breakdown=breakdown,
                material_code=row["material_code"],
                ref_unit_cost_eur=round(cost, 2) if cost is not None else None,
                ref_qty=ref_qty,
                ref_plant=plant,
            )
        )
    return out


def _dim_diff(component_score: float) -> float:
    """Relative size difference (larger − smaller) / larger, recovered from exp(-|ln a − ln b| / τ)."""
    if component_score <= 0:
        return 1.0
    return 1 - math.exp(SIZE_TAU * math.log(component_score))


def reuse_hint(spec: DrawingSpec, similar: list[SimilarPart], line_no: int) -> ValidationIssue | None:
    """REUSE-001 (INFO): score ≥ 0.92, same material and main dimensions within 5 % of the top part."""
    if not similar:
        return None
    top = max(similar, key=lambda s: s.score)
    code = spec.title_block.material_code
    if top.score < REUSE_MIN_SCORE or not code or code != top.material_code:
        return None
    bd = top.score_breakdown
    diffs = [_dim_diff(bd.get(k, 0.0)) for k in ("diameter", "length")]
    if any(x >= REUSE_MAX_DIM_DIFF for x in diffs):
        return None
    return ValidationIssue(
        code="REUSE-001",
        severity=Severity.INFO,
        line_no=line_no,
        field_path=None,
        message=(
            f"Nearly identical to historical part {top.part_number} ({top.title}): score {top.score:.2f}, "
            f"same material {code}, main dimensions within {max(diffs):.1%}."
        ),
        suggestion=(
            f"Reuse the routing, inspection plan and CNC programme of {top.part_number}; check only the "
            "drawing differences. Setup may be reduced because programmes and fixtures exist. "
            "Mention the reference part in internal notes only."
        ),
        kb_ref=REUSE_KB_REF,
    )
