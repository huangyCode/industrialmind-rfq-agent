"""Deterministic post-processing of an extracted DrawingSpec: units, material code, IT grades, envelope, checks."""

from __future__ import annotations

import bisect
import re

from rfq_agent.data.repositories import MaterialRepo
from rfq_agent.models import DrawingSpec, Envelope, FeatureType, ShapeClass

INCH_MM = 25.4

# ISO 286-1 standard tolerance grades (µm). Upper bounds of the nominal size ranges in mm (lower bound exclusive).
IT_RANGES_MM = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
IT_TABLE_UM: dict[int, tuple[int, ...]] = {
    5: (4, 5, 6, 8, 9, 11, 13, 15, 18, 20, 23, 25, 27),
    6: (6, 8, 9, 11, 13, 16, 19, 22, 25, 29, 32, 36, 40),
    7: (10, 12, 15, 18, 21, 25, 30, 35, 40, 46, 52, 57, 63),
    8: (14, 18, 22, 27, 33, 39, 46, 54, 63, 72, 81, 89, 97),
    9: (25, 30, 36, 43, 52, 62, 74, 87, 100, 115, 130, 140, 155),
    10: (40, 48, 58, 70, 84, 100, 120, 140, 160, 185, 210, 230, 250),
    11: (60, 75, 90, 110, 130, 160, 190, 220, 250, 290, 320, 360, 400),
}

_FIT_RE = re.compile(r"([A-Za-z]{1,2})\s*(\d{1,2})")


def it_tolerance_um(nominal_mm: float, grade: int) -> int | None:
    """Standard tolerance in µm, or None outside the table (size > 500 mm or grade outside IT5–IT11)."""
    if grade not in IT_TABLE_UM or nominal_mm <= 0 or nominal_mm > IT_RANGES_MM[-1]:
        return None
    return IT_TABLE_UM[grade][bisect.bisect_left(IT_RANGES_MM, nominal_mm)]


def it_grade(nominal_mm: float, upper: float | None, lower: float | None, fit: str | None) -> int | None:
    """IT grade from a fit code (digit of 'k6' / 'H7'; hole part of 'H7/g6') or else from the ± limit band."""
    if fit:
        m = _FIT_RE.search(fit)
        if m:
            return int(m.group(2))
    if upper is None or lower is None or not nominal_mm:
        return None
    band_um = abs(upper - lower) * 1000
    if band_um <= 0:
        return None
    candidates = [(g, it_tolerance_um(nominal_mm, g)) for g in IT_TABLE_UM]
    candidates = [(g, t) for g, t in candidates if t is not None]
    if not candidates:
        return None
    # nearest grade by band width; ties go to the coarser grade (the one the band actually fits in)
    return min(candidates, key=lambda gt: (abs(gt[1] - band_um), -gt[0]))[0]


def fit_limits(nominal_mm: float, fit: str) -> tuple[float, float] | None:
    """(upper, lower) in mm for fits whose fundamental deviation is zero or symmetric: H, h, JS, js."""
    m = _FIT_RE.fullmatch(fit.strip())
    if not m:
        return None
    letters, grade = m.group(1), int(m.group(2))
    t = it_tolerance_um(nominal_mm, grade)
    if t is None:
        return None
    t_mm = t / 1000
    match letters:
        case "H":
            return t_mm, 0.0
        case "h":
            return 0.0, -t_mm
        case "JS" | "js":
            return t_mm / 2, -t_mm / 2
    return None


def _convert_inch(spec: DrawingSpec) -> None:
    def cv(x: float | None) -> float | None:
        return None if x is None else round(x * INCH_MM, 4)

    for f in spec.features:
        f.nominal_mm, f.length_mm, f.face_width_mm = cv(f.nominal_mm), cv(f.length_mm), cv(f.face_width_mm)
        if f.tolerance:
            f.tolerance.upper, f.tolerance.lower = cv(f.tolerance.upper), cv(f.tolerance.lower)
    e = spec.envelope
    e.max_diameter_mm, e.length_mm = cv(e.max_diameter_mm), cv(e.length_mm)
    e.width_mm, e.height_mm = cv(e.width_mm), cv(e.height_mm)
    spec.title_block.units = "mm"
    spec.extraction_warnings.append("Drawing dimensioned in inch; all dimensions converted to mm.")


def _outer_size(f) -> float | None:
    if f.type == FeatureType.GEAR_TEETH and f.gear_module and f.gear_teeth:
        return f.gear_module * (f.gear_teeth + 2)  # tip diameter
    if f.type in (FeatureType.OUTER_DIAMETER, FeatureType.SPLINE):
        return f.nominal_mm
    return None


def _derive_envelope(spec: DrawingSpec) -> None:
    e: Envelope = spec.envelope
    if e.shape_class == ShapeClass.ROTATIONAL:
        ods = [d for f in spec.features if (d := _outer_size(f))]
        if e.max_diameter_mm is None and ods:
            e.max_diameter_mm = max(ods)
            spec.extraction_warnings.append(
                f"Envelope max diameter derived from features: {e.max_diameter_mm} mm."
            )
        if e.length_mm is None:
            steps = [
                f.length_mm for f in spec.features if f.type == FeatureType.OUTER_DIAMETER and f.length_mm
            ]
            if steps:
                e.length_mm = round(sum(steps), 3)
                spec.extraction_warnings.append(
                    f"Envelope length derived as sum of diameter step lengths: {e.length_mm} mm."
                )
        if e.max_diameter_mm is None or e.length_mm is None:
            spec.extraction_warnings.append("Envelope incomplete: overall diameter or length not found.")
    elif e.shape_class == ShapeClass.PRISMATIC:
        if None in (e.length_mm, e.width_mm, e.height_mm):
            spec.extraction_warnings.append("Envelope incomplete: overall length, width or height not found.")


def _checks(spec: DrawingSpec) -> None:
    e, w = spec.envelope, spec.extraction_warnings
    tol = 1.001
    for f in spec.features:
        size = _outer_size(f)
        if (
            e.shape_class == ShapeClass.ROTATIONAL
            and size
            and e.max_diameter_mm
            and size > e.max_diameter_mm * tol
        ):
            w.append(f"{f.id}: Ø{size} exceeds envelope max diameter {e.max_diameter_mm}.")
        if f.type in (FeatureType.BORE, FeatureType.HOLE) and f.nominal_mm:
            outer = e.max_diameter_mm or max(
                (x for x in (e.length_mm, e.width_mm, e.height_mm) if x), default=None
            )
            if outer and f.nominal_mm >= outer:
                w.append(f"{f.id}: hole/bore Ø{f.nominal_mm} not smaller than the part envelope.")
        if (
            f.length_mm
            and e.length_mm
            and f.length_mm > e.length_mm * tol
            and e.shape_class == ShapeClass.ROTATIONAL
        ):
            w.append(f"{f.id}: length {f.length_mm} exceeds overall length {e.length_mm}.")
        if f.type == FeatureType.THREAD and not f.thread_spec:
            w.append(f"{f.id}: thread without a thread designation.")
        if f.tolerance and f.tolerance.upper is not None and f.tolerance.lower is not None:
            if f.tolerance.upper < f.tolerance.lower:
                w.append(f"{f.id}: upper deviation {f.tolerance.upper} below lower {f.tolerance.lower}.")
        if f.evidence is None:
            w.append(f"{f.id}: no evidence text; treat as low confidence.")
    if e.shape_class == ShapeClass.ASSEMBLY and not spec.parts_list:
        w.append("Assembly drawing without a readable parts list.")
    item_nos = [p.item_no for p in spec.parts_list]
    if len(item_nos) != len(set(item_nos)):
        w.append("Parts list has duplicate item numbers.")


def postprocess(spec: DrawingSpec, materials: MaterialRepo) -> DrawingSpec:
    """Return a normalised copy of `spec`; findings are appended to extraction_warnings (deduplicated)."""
    spec = spec.model_copy(deep=True)
    if spec.title_block.units == "inch":
        _convert_inch(spec)
    if spec.parts_list and spec.envelope.shape_class != ShapeClass.ASSEMBLY:
        spec.extraction_warnings.append(
            f"Parts list with {len(spec.parts_list)} rows but shape class "
            f"'{spec.envelope.shape_class}'; set to assembly."
        )
        spec.envelope.shape_class = ShapeClass.ASSEMBLY

    tb = spec.title_block
    if tb.material:
        tb.material_code = materials.resolve(tb.material)
        # assemblies legitimately print e.g. "see parts list" in the material field
        if tb.material_code is None and spec.envelope.shape_class != ShapeClass.ASSEMBLY:
            spec.extraction_warnings.append(f"Material '{tb.material}' not found in material master data.")
    elif spec.envelope.shape_class != ShapeClass.ASSEMBLY:
        spec.extraction_warnings.append("No material specified in the title block.")

    for f in spec.features:
        t = f.tolerance
        if t is None or f.nominal_mm is None:
            continue
        if t.fit and (t.upper is None or t.lower is None) and (lim := fit_limits(f.nominal_mm, t.fit)):
            t.upper, t.lower = lim
        t.it_grade = it_grade(f.nominal_mm, t.upper, t.lower, t.fit)

    _derive_envelope(spec)
    _checks(spec)
    spec.extraction_warnings = list(dict.fromkeys(spec.extraction_warnings))
    return spec
