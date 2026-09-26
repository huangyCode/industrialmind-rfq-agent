"""Envelope / finished volume estimates and raw-stock selection (DESIGN §5.6, §5.7)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from rfq_agent.config import BIZ
from rfq_agent.models import DrawingSpec, FeatureType, ShapeClass

BAR_DIAMETER_ALLOWANCE_MM = 3.0
BAR_LENGTH_ALLOWANCE_MM = 5.0  # facing allowance + saw kerf
PLATE_ALLOWANCE_MM = 3.0
ROTATIONAL_FILL = 0.7  # finished / envelope when no OD segments were extracted
PRISMATIC_FILL = 0.6
MIN_FILL = 0.15  # lower clamp for feature-based estimates


@dataclass
class Stock:
    kind: Literal["bar", "plate", "none"]
    diameter_mm: float | None
    length_mm: float
    width_mm: float | None
    thickness_mm: float | None
    volume_cm3: float
    description: str


def _cyl_cm3(d_mm: float, l_mm: float) -> float:
    return math.pi / 4 * d_mm**2 * l_mm / 1000.0


def main_dims(spec: DrawingSpec) -> tuple[float, float, float, float]:
    """Return (max_diameter, length, width, height) in mm with feature-based fallbacks; 0 = unknown."""
    env = spec.envelope
    ods = [f for f in spec.features if f.type == FeatureType.OUTER_DIAMETER and f.nominal_mm]
    d = env.max_diameter_mm or max((f.nominal_mm for f in ods), default=0.0)
    length = env.length_mm or sum((f.length_mm or 0.0) * f.quantity for f in ods)
    return float(d or 0.0), float(length or 0.0), float(env.width_mm or 0.0), float(env.height_mm or 0.0)


def envelope_volume_cm3(spec: DrawingSpec) -> float:
    """Bounding cylinder (rotational) or bounding box (prismatic / assembly)."""
    d, length, w, h = main_dims(spec)
    if spec.envelope.shape_class == ShapeClass.ROTATIONAL:
        return _cyl_cm3(d, length)
    if spec.envelope.shape_class == ShapeClass.ASSEMBLY and d and not (w and h):
        return _cyl_cm3(d, length)
    return length * w * h / 1000.0


def _removed_by_holes_cm3(spec: DrawingSpec, default_depth: float) -> float:
    vol = 0.0
    for f in spec.features:
        if f.type in (FeatureType.BORE, FeatureType.HOLE) and f.nominal_mm:
            vol += _cyl_cm3(f.nominal_mm, f.length_mm or default_depth) * f.quantity
    return vol


def finished_volume_cm3(spec: DrawingSpec) -> float:
    """Rough finished-part volume. Rotational: Σ OD segments − bores; prismatic: box − pockets/holes."""
    env = envelope_volume_cm3(spec)
    if env <= 0:
        return 0.0
    d, length, w, h = main_dims(spec)
    shape = spec.envelope.shape_class
    if shape == ShapeClass.ROTATIONAL:
        segs = [
            f for f in spec.features if f.type == FeatureType.OUTER_DIAMETER and f.nominal_mm and f.length_mm
        ]
        if not segs:
            return env * ROTATIONAL_FILL
        vol = sum(_cyl_cm3(f.nominal_mm, f.length_mm) * f.quantity for f in segs)
        covered = sum(f.length_mm * f.quantity for f in segs)
        if length > covered:
            # un-dimensioned remainder (shoulders, transitions): assume the smallest listed diameter
            vol += _cyl_cm3(min(f.nominal_mm for f in segs), length - covered)
        vol -= _removed_by_holes_cm3(spec, default_depth=length)
    elif shape == ShapeClass.PRISMATIC:
        cut = _removed_by_holes_cm3(spec, default_depth=h or min(x for x in (w, length) if x) or 0)
        for f in spec.features:
            if f.type in (FeatureType.POCKET, FeatureType.SLOT) and f.nominal_mm and f.length_mm:
                depth = 0.5 * h if h else f.nominal_mm
                cut += f.nominal_mm * f.length_mm * depth / 1000.0 * f.quantity
        if cut <= 0:
            return env * PRISMATIC_FILL
        vol = env - cut
    else:
        return 0.0
    return min(max(vol, env * MIN_FILL), env)


def _round_up(value: float, series: tuple[int, ...], step: int = 10) -> tuple[float, bool]:
    """Smallest series value ≥ value; beyond the series round up to `step` (flagged non-standard)."""
    for s in series:
        if s >= value - 1e-9:
            return float(s), True
    return float(math.ceil(value / step) * step), False


def _num(x: float) -> str:
    return f"{x:g}"


def select_stock(spec: DrawingSpec, material_name: str | None = None) -> Stock:
    """Bar: Ø(max OD + 3) → standard series, length + 5. Plate: sides + 3, thickness → standard series."""
    shape = spec.envelope.shape_class
    mat = f" {material_name}" if material_name else ""
    d, length, w, h = main_dims(spec)
    if shape == ShapeClass.ASSEMBLY:
        return Stock("none", None, 0.0, None, None, 0.0, "no raw stock (assembly)")
    if shape == ShapeClass.ROTATIONAL:
        ds, std = _round_up(d + BAR_DIAMETER_ALLOWANCE_MM, BIZ.standard_bar_diameters)
        ls = length + BAR_LENGTH_ALLOWANCE_MM
        desc = f"Round bar{mat} Ø{_num(ds)} × {_num(ls)} mm" + ("" if std else " (non-standard diameter)")
        return Stock("bar", ds, ls, None, None, _cyl_cm3(ds, ls), desc)
    dims = sorted(x for x in (length, w, h) if x) or [0.0]
    while len(dims) < 3:
        dims.insert(0, dims[0])
    t_raw, w_raw, l_raw = dims
    ts, std = _round_up(t_raw + PLATE_ALLOWANCE_MM, BIZ.standard_plate_thickness)
    ws, ls = w_raw + PLATE_ALLOWANCE_MM, l_raw + PLATE_ALLOWANCE_MM
    desc = f"Plate{mat} {_num(ts)} × {_num(ws)} × {_num(ls)} mm" + (
        "" if std else " (non-standard thickness)"
    )
    return Stock("plate", None, ls, ws, ts, ls * ws * ts / 1000.0, desc)


def weight_kg(volume_cm3: float, density_g_cm3: float) -> float:
    return volume_cm3 * density_g_cm3 / 1000.0


# ISO 286 tolerance-grade factors (multiples of the standard tolerance unit i)
_IT_STEPS = (3, 6, 10, 18, 30, 50, 80, 120, 180, 250, 315, 400, 500)
_IT_FACTORS = {5: 7, 6: 10, 7: 16, 8: 25, 9: 40, 10: 64, 11: 100, 12: 160, 13: 250, 14: 400}


def feature_it_grade(nominal_mm: float | None, tol) -> int | None:
    """IT grade: post-processed value → digits of the fit code → estimate from the tolerance band."""
    if tol is None:
        return None
    if tol.it_grade is not None:
        return tol.it_grade
    if tol.fit:
        m = re.search(r"(\d{1,2})\s*$", tol.fit.strip())
        if m:
            return int(m.group(1))
    if nominal_mm and tol.upper is not None and tol.lower is not None:
        band_um = abs(tol.upper - tol.lower) * 1000.0
        lo, hi = 1.0, 3.0  # ISO 286 nominal-size step containing the diameter
        for b in _IT_STEPS:
            if nominal_mm <= b:
                hi = b
                break
            lo = b
        d_mean = math.sqrt(lo * max(hi, lo))
        i_um = 0.45 * d_mean ** (1 / 3) + 0.001 * d_mean
        for grade, k in _IT_FACTORS.items():
            if band_um <= k * i_um * 1.05:
                return grade
        return 16
    return None
