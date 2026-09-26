"""Synthetic history: ~90 historical parts with full DrawingSpecs, "actual" MES routings, ERP costs, quotes.

Everything is fictional and generated with a fixed seed. Three layers:

1. **Part specs** — per family (shaft, flange/cover, bracket, housing, gear, assembly) a parametric
   generator builds a complete, valid `DrawingSpec` (features with fits/Ra, heat & surface treatment,
   parts list for assemblies). Specs go to `data/history/specs.jsonl` (for leave-one-out backtests) and
   into `parts.spec_json`; the other `parts` columns are derived from the spec.
2. **Hidden process model** (`hidden_routing`) — the stand-in for "what really happened on the shop floor".
   It is deliberately *independent* of the rule engine (`routing_rules.py`) and uses a different
   functional form plus factors the rules cannot see (see its docstring). Its output is written to
   `routings` as the MES actual times.
3. **Costs & quotes** — `part_costs` are computed with the real costing engine (`costing.compute_costs`)
   on the *hidden* routing (adjusted per plant crew and batch size) and a BOM from `bom.build_bom`;
   `quote_history` adds 1–3 historical quotes per part.

Anchor parts are placed by hand so the four demo RFQs behave as designed (`anchors()`); a generated
variant that would out-rank an anchor against a demo drawing is re-drawn (`all_drafts()`).
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from rfq_agent.agents.bom import build_bom
from rfq_agent.agents.costing import compute_costs
from rfq_agent.agents.geometry import feature_it_grade, finished_volume_cm3, weight_kg
from rfq_agent.agents.postprocess import postprocess
from rfq_agent.agents.similarity import featurize_spec, part_columns, score_features
from rfq_agent.config import DATA_DIR
from rfq_agent.data.repositories import CatalogRepo, MaterialRepo, PartRepo, RateRepo, ServiceRepo
from rfq_agent.models import (
    DrawingSpec,
    Envelope,
    Evidence,
    Feature,
    FeatureType,
    PartsListItem,
    RoutingOp,
    ShapeClass,
    TitleBlock,
    Tolerance,
)

SEED = 20260926
HISTORY_DIR = DATA_DIR / "history"
SPECS_FILE = HISTORY_DIR / "specs.jsonl"
PLANTS = ("DE", "PL", "CN")
QTYS = (50, 200, 500, 1000)
COST_DATE = date(2026, 6, 30)
OUTSOURCED_WC = "OUTSOURCED"

MATERIAL_TEXT = {
    "C45": ["C45", "C45E", "C45+N", "1.0503"],
    "42CrMo4": ["42CrMo4+QT", "42CrMo4", "1.7225"],
    "16MnCr5": ["16MnCr5", "1.7131"],
    "AW6082": ["EN AW-6082 T6", "AW-6082", "6082-T6"],
    "GJS500": ["EN-GJS-500-7", "GJS-500-7"],
    "X5CrNi18-10": ["X5CrNi18-10", "1.4301"],
}
DRAWERS = ["A. Kessler", "B. Roth", "C. Maurer", "D. Wendt", "E. Falk", "F. Brandt", "G. Lorenz", "H. Vogt"]
CUSTOMERS = [
    "Hallvig Fördertechnik GmbH",
    "Norvane Motion Systems Ltd",
    "Tervalo Automation B.V.",
    "Veltrum Aktorik GmbH",
    "Ostrava Pumpentechnik s.r.o.",
    "Kjellberg Drives AB",
    "Morandi Macchine S.p.A.",
    "Brightwater Robotics Ltd",
    "Lindqvist Hydraulik GmbH",
    "Pelgrim Packaging B.V.",
]


# =====================================================================================================
# 1. Spec builders
# =====================================================================================================


@dataclass
class Draft:
    """A historical part before it gets a part_id: spec + generation-only facts the drawing does not show."""

    part_number: str
    family: str
    spec: DrawingSpec
    latent: dict = field(default_factory=dict)  # hidden facts: pocket depths, clampings, noise, …


class FB:
    """Feature list builder with sequential ids and verbatim evidence."""

    def __init__(self) -> None:
        self.features: list[Feature] = []

    def add(
        self,
        type_: FeatureType,
        desc: str,
        ev: str,
        *,
        nominal: float | None = None,
        length: float | None = None,
        qty: int = 1,
        fit: str | None = None,
        upper: float | None = None,
        lower: float | None = None,
        ra: float | None = None,
        thread: str | None = None,
        module: float | None = None,
        teeth: int | None = None,
        face_width: float | None = None,
        loc: str = "front view",
    ) -> None:
        tol = None
        if fit or upper is not None or lower is not None:
            tol = Tolerance(fit=fit, upper=upper, lower=lower)
        self.features.append(
            Feature(
                id=f"F{len(self.features) + 1}",
                type=type_,
                description=desc,
                nominal_mm=nominal,
                length_mm=length,
                quantity=qty,
                tolerance=tol,
                ra_um=ra,
                thread_spec=thread,
                gear_module=module,
                gear_teeth=teeth,
                face_width_mm=face_width,
                evidence=Evidence(text=ev, location=loc),
            )
        )


def _g(x: float) -> str:
    return f"{x:g}"


def _title_block(
    rng: random.Random, pn: str, title: str, material_text: str, *, default_ra: float | None = 3.2
) -> TitleBlock:
    rev = rng.choice("ABCD")
    drawn = rng.choice(DRAWERS)
    day = date(2021, 1, 4) + timedelta(days=rng.randrange(0, 5 * 365))
    tb = TitleBlock(
        part_number=pn,
        revision=rev,
        title=title,
        material=material_text,
        general_tolerance="ISO 2768-mK",
        default_ra_um=default_ra,
        scale=rng.choice(["1:1", "1:2", "2:1"]),
        units="mm",
        drawn_by=drawn,
        date=day.isoformat(),
    )
    ev = {
        "part_number": pn,
        "revision": rev,
        "title": title,
        "material": material_text,
        "general_tolerance": "ISO 2768-mK",
        "scale": tb.scale,
        "drawn_by": drawn,
        "date": tb.date,
    }
    if default_ra is not None:
        ev["default_ra_um"] = f"Ra {_g(default_ra)}"
    tb.evidence = {k: Evidence(text=v, location="title block") for k, v in ev.items()}
    return tb


def _spec(
    rng: random.Random,
    pn: str,
    title: str,
    mat_code: str | None,
    envelope: Envelope,
    fb: FB | None,
    *,
    ht: str | None = None,
    surface: str | None = None,
    notes: list[str] | None = None,
    parts_list: list[PartsListItem] | None = None,
    material_text: str | None = None,
) -> DrawingSpec:
    mtext = material_text or (rng.choice(MATERIAL_TEXT[mat_code]) if mat_code else "see parts list")
    tb = _title_block(rng, pn, title, mtext, default_ra=None if parts_list else 3.2)
    tb.material_code = mat_code
    return DrawingSpec(
        drawing_file=f"{pn}.pdf",
        title_block=tb,
        envelope=envelope,
        features=fb.features if fb else [],
        heat_treatment=ht,
        surface_treatment=surface,
        notes=notes or [],
        parts_list=parts_list or [],
    )


def _keyway_width(d: float) -> int:
    for lim, b in ((12, 4), (17, 5), (22, 6), (30, 8), (38, 10), (44, 12), (50, 14), (58, 16), (65, 18)):
        if d <= lim:
            return b
    return 20


def make_shaft(
    rng: random.Random,
    pn: str,
    title: str,
    mat: str,
    d: float,
    length: float,
    *,
    seat_fit: str = "k6",
    seat_ra: float | None = 0.8,
    journal_fit: str = "h7",
    keyways: int = 1,
    grooves: int = 0,
    center_threads: int = 1,
    radial_threads: int = 0,
    cross_holes: int = 0,
    extra_seat: tuple[str, float] | None = None,
    pinion: tuple[float, float] | None = None,
    ht: str | None = None,
    surface: str | None = None,
    material_text: str | None = None,
    latent: dict | None = None,
) -> Draft:
    """Stepped shaft: journal (with keyway) – seat – body – seat; optional grooves, holes, threads."""
    fb = FB()
    seat = float(max(10, 5 * round((d - 4) / 5)))
    journal = float(max(8, seat - (3 if seat <= 25 else 5)))
    l_j = round(0.22 * length / 5) * 5
    l_s = max(10.0, round(0.11 * length / 5) * 5)
    l_body = length - l_j - 2 * l_s
    fb.add(
        FeatureType.OUTER_DIAMETER,
        f"Drive journal Ø{_g(journal)} {journal_fit}",
        f"Ø{_g(journal)} {journal_fit}",
        nominal=journal,
        length=l_j,
        fit=journal_fit,
    )
    for _ in range(keyways):
        b = _keyway_width(journal)
        kl = max(10.0, l_j - 10)
        fb.add(
            FeatureType.KEYWAY,
            f"Parallel keyway {b} P9 on journal, length {_g(kl)}",
            f"{b} P9",
            nominal=float(b),
            length=kl,
            fit="P9",
            loc="section A-A",
        )
    for side in ("drive side", "non-drive side"):
        fb.add(
            FeatureType.OUTER_DIAMETER,
            f"Bearing seat Ø{_g(seat)} {seat_fit}"
            + (f", Ra {_g(seat_ra)}" if seat_ra else "")
            + f" ({side})",
            f"Ø{_g(seat)} {seat_fit}",
            nominal=seat,
            length=l_s,
            fit=seat_fit,
            ra=seat_ra,
        )
    if extra_seat:
        fit, ra = extra_seat
        es = seat + 1
        fb.add(
            FeatureType.OUTER_DIAMETER,
            f"Encoder seat Ø{_g(es)} {fit}, Ra {_g(ra)}",
            f"Ø{_g(es)} {fit}",
            nominal=es,
            length=8.0,
            fit=fit,
            ra=ra,
        )
        l_body -= 8
    fb.add(FeatureType.OUTER_DIAMETER, f"Shaft body Ø{_g(d)}", f"Ø{_g(d)}", nominal=d, length=l_body)
    if pinion:
        m, b = pinion
        z = int(d // m) - 2
        fb.add(
            FeatureType.GEAR_TEETH,
            f"Integral pinion teeth m {_g(m)}, z {z}, face width {_g(b)}",
            f"m = {_g(m)}, z = {z}",
            nominal=m * z,
            module=m,
            teeth=z,
            face_width=b,
            loc="gear data",
        )
    for i in range(grooves):
        fb.add(
            FeatureType.GROOVE,
            f"Retaining ring groove for DIN 471-{_g(seat)}",
            f"groove DIN 471-{_g(seat)}" + (f" ({i + 1})" if grooves > 1 else ""),
            nominal=seat,
            length=1.3,
        )
    if center_threads:
        m = 6 if journal < 20 else 8 if journal < 28 else 10 if journal < 36 else 12
        fb.add(
            FeatureType.THREAD,
            f"Tapped centre hole M{m} in end face, thread depth {2 * m}",
            f"M{m} depth {2 * m}",
            nominal=float(m),
            length=float(2 * m),
            qty=center_threads,
            thread=f"M{m}",
        )
    if radial_threads:
        fb.add(
            FeatureType.THREAD,
            f"{radial_threads}x tapped hole M6 radial in shaft body, depth 10",
            f"{radial_threads}× M6 depth 10",
            nominal=6.0,
            length=10.0,
            qty=radial_threads,
            thread="M6",
        )
    if cross_holes:
        fb.add(
            FeatureType.HOLE,
            f"{cross_holes}x cross hole Ø6 through body",
            f"{cross_holes}× Ø6 THRU",
            nominal=6.0,
            length=d,
            qty=cross_holes,
        )
    fb.add(FeatureType.CHAMFER, "Chamfer 1x45° at both shaft ends", "2× 1×45°", nominal=1.0, qty=2)
    notes = ["Break all sharp edges 0.2-0.5."]
    if ht:
        notes.insert(0, f"Heat treatment: {ht}.")
    spec = _spec(
        rng,
        pn,
        title,
        mat,
        Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=d, length_mm=float(length)),
        fb,
        ht=ht,
        surface=surface,
        notes=notes,
        material_text=material_text,
    )
    return Draft(pn, "shaft", spec, latent or {})


def make_flange(
    rng: random.Random,
    pn: str,
    title: str,
    mat: str,
    d: float,
    thick: float,
    *,
    recess: float | None,
    recess_fit: str = "H7",
    bore: float | None,
    n_threads: int = 4,
    thread_m: int = 6,
    n_holes: int = 4,
    hole_d: float = 9.0,
    pcd: float | None = None,
    face_tol: float | None = 0.1,
    surface: str | None = None,
    ht: str | None = None,
    family: str = "flange",
    latent: dict | None = None,
) -> Draft:
    fb = FB()
    pcd = pcd or round(0.83 * d / 5) * 5
    fb.add(
        FeatureType.OUTER_DIAMETER, f"Flange outer diameter Ø{_g(d)}", f"Ø{_g(d)}", nominal=d, length=thick
    )
    if recess:
        fb.add(
            FeatureType.BORE,
            f"Centring recess Ø{_g(recess)} {recess_fit}, depth 4, Ra 1.6",
            f"Ø{_g(recess)} {recess_fit}",
            nominal=recess,
            length=4.0,
            fit=recess_fit,
            ra=1.6,
            loc="section A-A",
        )
    if bore:
        fb.add(
            FeatureType.BORE,
            f"Through bore Ø{_g(bore)}",
            f"Ø{_g(bore)}",
            nominal=bore,
            length=thick - (4 if recess else 0),
            loc="section A-A",
        )
    if n_threads:
        fb.add(
            FeatureType.THREAD,
            f"{n_threads}x tapped hole M{thread_m} on PCD {_g(pcd)}, thread depth {2 * thread_m}",
            f"{n_threads}× M{thread_m} on PCD {_g(pcd)}",
            nominal=float(thread_m),
            length=float(2 * thread_m),
            qty=n_threads,
            thread=f"M{thread_m}",
        )
    if n_holes:
        fb.add(
            FeatureType.HOLE,
            f"{n_holes}x through hole Ø{_g(hole_d)} on PCD {_g(pcd)}",
            f"{n_holes}× Ø{_g(hole_d)} THRU on PCD {_g(pcd)}",
            nominal=hole_d,
            length=thick,
            qty=n_holes,
        )
    if face_tol:
        fb.add(
            FeatureType.FACE,
            f"Flange thickness {_g(thick)} ±{_g(face_tol)}",
            f"{_g(thick)} ±{_g(face_tol)}",
            nominal=thick,
            upper=face_tol,
            lower=-face_tol,
            loc="section A-A",
        )
    fb.add(FeatureType.CHAMFER, "Chamfer 1x45° on outer edges", "1×45°", nominal=1.0, loc="section A-A")
    notes = ["Break all sharp edges 0.2-0.5."]
    if surface:
        notes.insert(0, f"Surface treatment: {surface}.")
    spec = _spec(
        rng,
        pn,
        title,
        mat,
        Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=d, length_mm=thick),
        fb,
        ht=ht,
        surface=surface,
        notes=notes,
    )
    return Draft(pn, family, spec, latent or {})


def make_block(
    rng: random.Random,
    pn: str,
    title: str,
    mat: str,
    size: tuple[float, float, float],
    *,
    family: str,
    pockets: list[tuple[float, float, float]] = (),  # (width, length, depth)
    wall: float | None = None,
    holes: list[tuple[int, float, float, str | None]] = (),  # (qty, Ø, depth, fit)
    threads: list[tuple[int, int, float]] = (),  # (qty, M, depth)
    bores: list[tuple[float, float, str, float | None]] = (),  # (Ø, depth, fit, Ra)
    flat_face: float | None = None,
    slots: int = 0,
    surface: str | None = None,
    latent: dict | None = None,
) -> Draft:
    """Prismatic part (bracket / housing)."""
    length, width, height = size
    fb = FB()
    for w, ln, dep in pockets:
        fb.add(
            FeatureType.POCKET,
            f"Pocket {_g(ln)} x {_g(w)}, depth {_g(dep)}, corner radius R6",
            f"POCKET {_g(ln)}×{_g(w)} depth {_g(dep)}, R6",
            nominal=w,
            length=ln,
            loc="top view",
        )
    if wall:
        fb.add(FeatureType.WALL, f"Side wall {_g(wall)}", f"WALL {_g(wall)}", nominal=wall, length=length)
    for dia, dep, fit, ra in bores:
        fb.add(
            FeatureType.BORE,
            f"Bearing bore Ø{_g(dia)} {fit}" + (f", Ra {_g(ra)}" if ra else ""),
            f"Ø{_g(dia)} {fit}",
            nominal=dia,
            length=dep,
            fit=fit,
            ra=ra,
            loc="section A-A",
        )
    for q, dia, dep, fit in holes:
        what = "dowel hole" if fit else "hole"
        fb.add(
            FeatureType.HOLE,
            f"{q}x {what} Ø{_g(dia)}" + (f" {fit}" if fit else "") + f", depth {_g(dep)}",
            f"{q}× Ø{_g(dia)}" + (f" {fit}" if fit else "") + f" depth {_g(dep)}",
            nominal=dia,
            length=dep,
            qty=q,
            fit=fit,
            loc="top view",
        )
    for q, m, dep in threads:
        fb.add(
            FeatureType.THREAD,
            f"{q}x tapped hole M{m}, thread depth {_g(dep)}",
            f"{q}× M{m} depth {_g(dep)}",
            nominal=float(m),
            length=dep,
            qty=q,
            thread=f"M{m}",
            loc="top view",
        )
    for i in range(slots):
        fb.add(
            FeatureType.SLOT,
            f"Slot 10 x 30 for adjustment ({i + 1})",
            f"SLOT 10×30 ({i + 1})",
            nominal=10.0,
            length=30.0,
            loc="top view",
        )
    if flat_face:
        fb.add(
            FeatureType.FACE,
            f"Mounting face, height {_g(height)} ±{_g(flat_face)}",
            f"{_g(height)} ±{_g(flat_face)}",
            nominal=height,
            upper=flat_face,
            lower=-flat_face,
        )
    notes = ["Break all sharp edges 0.3×45°."]
    if surface:
        notes.insert(0, f"Surface treatment: {surface}.")
    spec = _spec(
        rng,
        pn,
        title,
        mat,
        Envelope(shape_class=ShapeClass.PRISMATIC, length_mm=length, width_mm=width, height_mm=height),
        fb,
        surface=surface,
        notes=notes,
    )
    lat = {"pocket_depths": [p[2] for p in pockets]}
    lat.update(latent or {})
    return Draft(pn, family, spec, lat)


def make_gear(
    rng: random.Random,
    pn: str,
    mat: str,
    module: float,
    z: int,
    b: float,
    *,
    bore: float,
    bore_ra: float = 1.6,
    ht: str | None,
    keyway: bool = True,
    latent: dict | None = None,
) -> Draft:
    fb = FB()
    tip = module * (z + 2)
    fb.add(
        FeatureType.GEAR_TEETH,
        f"Spur gear teeth m {_g(module)}, z {z}, pressure angle 20°, face width {_g(b)}",
        f"m = {_g(module)}, z = {z}",
        nominal=module * z,
        module=module,
        teeth=z,
        face_width=b,
        loc="gear data",
    )
    fb.add(
        FeatureType.OUTER_DIAMETER,
        f"Tip diameter Ø{_g(tip)} h11",
        f"Ø{_g(tip)} h11",
        nominal=tip,
        length=b,
        fit="h11",
        loc="section A-A",
    )
    fb.add(
        FeatureType.BORE,
        f"Bore Ø{_g(bore)} H7, Ra {_g(bore_ra)}",
        f"Ø{_g(bore)} H7",
        nominal=bore,
        length=b,
        fit="H7",
        ra=bore_ra,
    )
    if keyway:
        kb = _keyway_width(bore)
        fb.add(
            FeatureType.KEYWAY,
            f"Hub keyway {kb} JS9 through",
            f"{kb} JS9",
            nominal=float(kb),
            length=b,
            fit="JS9",
        )
    fb.add(FeatureType.CHAMFER, "Chamfer 1x45° on tip edges", "2× 1×45°", nominal=1.0, qty=2)
    notes = ["Break all sharp edges 0.2-0.5."]
    if ht:
        notes.insert(0, f"Heat treatment: {ht}.")
    title = "Spur Gear" if z > 24 else "Pinion"
    spec = _spec(
        rng,
        pn,
        title,
        mat,
        Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=tip, length_mm=b),
        fb,
        ht=ht,
        notes=notes,
    )
    return Draft(pn, "gear", spec, latent or {})


def make_assembly(
    rng: random.Random,
    pn: str,
    title: str,
    envelope: tuple[float, float],
    items: list[tuple[str | None, str, int, str | None, str | None]],
) -> Draft:
    """items: (part_number, description, qty, material, standard)."""
    pl = [
        PartsListItem(item_no=i, part_number=p, description=d, quantity=q, material=m, standard=s)
        for i, (p, d, q, m, s) in enumerate(items, start=1)
    ]
    d, length = envelope
    spec = _spec(
        rng,
        pn,
        title,
        None,
        Envelope(shape_class=ShapeClass.ASSEMBLY, max_diameter_mm=d, length_mm=length),
        None,
        notes=["Shaft must rotate freely by hand after assembly; 100 % functional test."],
        parts_list=pl,
    )
    return Draft(pn, "assembly", spec, {})


# =====================================================================================================
# 2. Hidden process model
# =====================================================================================================

# Shop-floor knowledge that is NOT in master data (the rule engine only sees `machinability`).
HARDNESS_HB = {"C45": 190, "42CrMo4": 220, "16MnCr5": 175, "AW6082": 95, "GJS500": 185, "X5CrNi18-10": 180}
QT_HARDNESS_HB = 290  # QT steels are finish-turned in the quenched & tempered state
CHIP_FACTOR = {"stainless": 1.8, "cast_iron": 0.75, "aluminium": 0.8}
FAMILY_BIAS = {
    "shaft": 1.10,
    "flange": 0.50,
    "bracket": 1.00,
    "housing": 1.20,
    "gear": 0.75,
    "assembly": 0.95,
}
CREW_FACTOR = {"DE": 1.00, "PL": 1.06, "CN": 1.12}
PART_SIGMA = 0.12
OP_SIGMA = 0.05
CHANGEOVER_MIN = 8.0  # per in-house machine op and batch (cleaning, first-off check)
MES_REF_QTY = 200  # routings table stores MES averages observed at this typical batch size


@dataclass
class HOp:
    op_code: str
    work_center: str
    setup: float
    cycle: float  # base minutes at the part's "true" speed (before batch / crew effects)
    service_code: str | None = None

    @property
    def outsourced(self) -> bool:
        return self.work_center == OUTSOURCED_WC


def learning(qty: int) -> float:
    """Small-batch cycle penalty (first-off parts, operator learning): 1.35 at 50 pcs → 1.08 at 1000."""
    return 1 + 0.35 * (50 / qty) ** 0.5


def _mat_factor(code: str | None, group: str | None, ht: str | None) -> float:
    hb = HARDNESS_HB.get(code or "", 200)
    t = (ht or "").lower()
    if group == "steel" and ("qt" in t or "temper" in t):
        hb = QT_HARDNESS_HB
    return (hb / 200) ** 1.7 * CHIP_FACTOR.get(group or "", 1.0)


def _it(f: Feature) -> int | None:
    return feature_it_grade(f.nominal_mm, f.tolerance)


def _ht_service(ht: str | None) -> str | None:
    t = (ht or "").lower()
    if not t:
        return None
    return "HT_CASE" if ("case" in t or "carbur" in t) else "HT_QT"


def _surface_service(st: str | None) -> str | None:
    t = (st or "").lower()
    if not t:
        return None
    if "anod" in t:
        return "ANODIZE"
    if "zinc" in t:
        return "ZINC"
    return "BLACK" if "black" in t else None


def hidden_routing(draft: Draft, material, rng: random.Random) -> list[HOp]:
    """Hidden "true" process model — what the MES would have recorded. Independent of routing_rules.py.

    Functional form (base minutes, before batch and crew effects):
      material factor mf = (HB_eff / 200)^1.7 × chip factor
          HB_eff from a hardness table the rules never see; QT steels are finish-turned at ~290 HB;
          chip factor: stainless 1.8 (work hardening), cast iron 0.75, aluminium 0.8.
          The rules instead divide removed volume linearly by the master-data machinability.
      TURN   setup 18 + 7·n_diam^0.7 (+12 bores, +10 tailstock if L/D > 4)
             cycle 0.6 + mf·(0.011·A_turned + 0.012·V_removed^0.9) + 0.25·n_steps + 0.35·grooves
             A_turned = turned mantle + faced area + bore mantles (cm²) — surface-driven, not volume-driven,
             so thin large-diameter flanges and long slender shafts behave differently from the rules.
      MILL   setup 30 + 14·clampings (latent 2–4); cycle 1.2 + mf·(0.006·A_faces + 0.035·V_removed^0.85)
             + 0.8·n_pockets^1.2 + deep pockets (latent depth) + holes 0.22·q·(1 + (L/D)^1.4/6) + 0.45·threads
      DRILL  (rotational) same hole law as MILL, setup 10 + 5 per hole pattern
      KEYWAY (0.8 + 0.04·length)·mf^0.7; HOB 0.1·z·b/10·m^0.7·mf^0.6
      GRIND  IT ≤ 6 / Ra ≤ 0.8, plus H7 bores of case-hardened parts; per ground feature
             0.9 + 0.02·area_cm², ×1.3 hardened / ×1.15 QT
      INSPECT CMM when ≥ 2 features ≤ IT7, any ≤ IT6, or any housing; else manual
    Family-specific extra ops (never generated by the rules → appear as calibration suggestions):
      shafts with heat treatment and L/D > 4.5 → STRAIGHTEN (press + dial gauge on the INSPECT bench)
      cast-iron housings → MILL_FINISH second setup after stress relief ageing, always CMM inspection
      gears → TOOTH_CHAMFER on the deburring bench
    Multipliers on every in-house op: FAMILY_BIAS (0.50 flange … 1.20 housing; a stand-in for family-
    specific practice such as fixtures, programmes and tool life), per-part lognormal noise σ = 0.12
    (anchors use a fixed value) and per-op noise σ = 0.05. Setup gets a fixed CHANGEOVER_MIN per batch.
    Applied later when costing: learning(qty) small-batch cycle penalty (1.35 at 50 pcs … 1.08 at 1000)
    and CREW_FACTOR per plant (DE 1.00 / PL 1.06 / CN 1.12).

    Net effect (DE, 200 pcs, cost incl. material/outsourcing): the rule engine alone is biased per family
    by roughly −21 % (shafts) … +21 % (flanges) but correlated with the truth, so blending with a similar
    part's actual times recovers part of the gap. Ops the rules never generate (STRAIGHTEN, MILL_FINISH,
    CMM on simple housings) only surface as suggestions — calibration cannot price them.
    """
    spec = draft.spec
    fam = draft.family
    lat = draft.latent
    code = spec.title_block.material_code
    group = material.mat_group if material else None
    mf = _mat_factor(code, group, spec.heat_treatment)
    feats = spec.features
    env = spec.envelope

    def of(*types: FeatureType) -> list[Feature]:
        return [f for f in feats if f.type in types]

    def qn(fs: list[Feature]) -> int:
        return sum(f.quantity for f in fs)

    def hole_time(holes: list[Feature], threads: list[Feature]) -> float:
        t = 0.0
        for f in holes:
            ratio = (f.length_mm or 2 * (f.nominal_mm or 5)) / (f.nominal_mm or 5)
            t += 0.22 * f.quantity * (1 + ratio**1.4 / 6) * mf**0.5
        return t + 0.45 * qn(threads) * mf**0.4

    ops: list[HOp] = []
    if env.shape_class == ShapeClass.ASSEMBLY:
        items = spec.parts_list
        fasteners = sum(i.quantity for i in items if (i.standard or "") in ("ISO 4762", "DIN 471", "DIN 472"))
        bearings = sum(i.quantity for i in items if (i.standard or "") == "DIN 625")
        n_parts = sum(i.quantity for i in items) - fasteners
        ops += [
            HOp("ASSEMBLY", "ASSEMBLY", 15, 1.6 * n_parts**0.9 + 0.4 * fasteners + 1.2 * bearings),
            HOp("TEST", "TEST", 8, 3.0 + 0.8 * bearings),
            HOp("WASH_PACK", "WASH_PACK", 0, 0.4),
        ]
    else:
        rot = env.shape_class == ShapeClass.ROTATIONAL
        d = env.max_diameter_mm or 0.0
        length = env.length_mm or 0.0
        if rot:
            ds, ls = 5 * math.ceil((d + 4) / 5), length + 6
            v_stock = math.pi / 4 * ds**2 * ls / 1000
            a_cut = math.pi / 4 * ds**2 / 100
            ods = [f for f in of(FeatureType.OUTER_DIAMETER) if f.nominal_mm and f.length_mm]
            bores = [f for f in of(FeatureType.BORE) if f.nominal_mm]
            mantle = sum(math.pi * f.nominal_mm * f.length_mm * f.quantity for f in ods) / 100
            faces = 2 * math.pi / 4 * d**2 / 100
            bore_a = sum(math.pi * f.nominal_mm * (f.length_mm or length) for f in bores) / 100
            v_fin = sum(math.pi / 4 * f.nominal_mm**2 * f.length_mm * f.quantity for f in ods) / 1000
            v_fin -= sum(math.pi / 4 * f.nominal_mm**2 * (f.length_mm or length) for f in bores) / 1000
            v_rem = max(v_stock - max(v_fin, 0.2 * v_stock), 0.0)
            n_diam = len({f.nominal_mm for f in ods}) or 1
            setup = 18 + 7 * n_diam**0.7 + (12 if bores else 0) + (10 if d and length / d > 4 else 0)
            cycle = (
                0.6
                + mf * (0.011 * (mantle + faces + bore_a) + 0.012 * v_rem**0.9)
                + 0.25 * len(ods)
                + 0.35 * qn(of(FeatureType.GROOVE))
            )
            ops.append(HOp("SAW", "SAW", 6, 0.3 + 0.06 * a_cut * mf**0.5))
            ops.append(HOp("TURN", "CNC_TURN", setup, cycle))
            holes = of(FeatureType.HOLE)
            threads = of(FeatureType.THREAD)
            if holes or threads:
                ops.append(HOp("DRILL", "DRILL", 10 + 5 * len(holes + threads), hole_time(holes, threads)))
        else:
            length, w, h = env.length_mm or 0.0, env.width_mm or 0.0, env.height_mm or 0.0
            dims = sorted((length + 4, w + 4, h + 4))
            v_stock = dims[0] * dims[1] * dims[2] / 1000
            a_cut = dims[0] * dims[1] / 100
            a_faces = 2 * (length * w + length * h + w * h) / 100
            pockets = of(FeatureType.POCKET)
            depths = lat.get("pocket_depths") or [0.5 * h] * len(pockets)
            v_pock = sum(
                (p.nominal_mm or 0) * (p.length_mm or 0) * dp / 1000
                for p, dp in zip(pockets, depths, strict=True)
            )
            cast = group == "cast_iron"
            v_rem = (v_stock - length * w * h / 1000) + v_pock
            if cast:
                v_rem *= 0.35  # near-net casting: only machining allowances are removed
            clampings = lat.get("clampings", 2)
            deep = sum(dp / 20 for dp in depths)
            holes = of(FeatureType.HOLE, FeatureType.BORE)
            cycle = (
                1.2
                + mf * (0.006 * a_faces + 0.035 * v_rem**0.85)
                + 0.8 * len(pockets) ** 1.2
                + deep
                + 0.3 * qn(of(FeatureType.SLOT))
                + hole_time(holes, of(FeatureType.THREAD))
            )
            ops.append(HOp("SAW", "SAW", 6, 0.3 + 0.06 * a_cut * mf**0.5) if not cast else None)
            ops.append(HOp("MILL", "CNC_MILL_3AX", 30 + 14 * clampings, cycle))
            if cast and fam == "housing":
                ops.append(HOp("MILL_FINISH", "CNC_MILL_3AX", 25, 0.4 * cycle))
        ops = [o for o in ops if o is not None]
        for f in of(FeatureType.KEYWAY):
            ops.append(HOp("KEYWAY", "CNC_MILL_3AX", 15, (0.8 + 0.04 * (f.length_mm or 20)) * mf**0.7))
        for g in of(FeatureType.GEAR_TEETH):
            z, b, m = g.gear_teeth or 20, g.face_width_mm or 20, g.gear_module or 2
            soft = _mat_factor(code, group, None)
            ops.append(HOp("HOB", "HOB", 30 + 0.3 * z, 0.1 * z * b / 10 * m**0.7 * soft**0.6))
            ops.append(HOp("TOOTH_CHAMFER", "DEBURR", 5, 0.025 * z))
        ht_code = _ht_service(spec.heat_treatment)
        if ht_code:
            ops.append(HOp("HEAT_TREAT", OUTSOURCED_WC, 0, 0, ht_code))
        if ht_code and fam == "shaft" and d and length / d > 4.5:
            ops.append(HOp("STRAIGHTEN", "INSPECT", 5, 1.0 + 0.008 * length))
        ground = []
        for f in feats:
            it = _it(f)
            if (it is not None and it <= 6) or (f.ra_um is not None and f.ra_um <= 0.8):
                ground.append(f)
            elif ht_code == "HT_CASE" and f.type == FeatureType.BORE and it is not None and it <= 7:
                ground.append(f)
        if ground:
            hard = 1.3 if ht_code == "HT_CASE" else 1.15 if ht_code else 1.0
            t = sum(
                (0.9 + 0.02 * math.pi * (f.nominal_mm or 20) * (f.length_mm or 20) / 100) * f.quantity
                for f in ground
            )
            wc = "GRIND_CYL" if rot else "GRIND_SURF"
            ops.append(HOp("GRIND", wc, 18 + 5 * len(ground), t * hard))
        sc = _surface_service(spec.surface_treatment)
        if sc:
            ops.append(HOp("SURFACE", OUTSOURCED_WC, 0, 0, sc))
        n_feat = qn(feats)
        burr = 1.4 if group == "aluminium" else 0.8 if group == "cast_iron" else 1.0
        ops.append(HOp("DEBURR", "DEBURR", 0, (0.35 + 0.07 * n_feat**0.9) * burr))
        tight = [f for f in feats if (_it(f) or 99) <= 7]
        if qn(tight) >= 2 or any((_it(f) or 99) <= 6 for f in feats) or fam == "housing":
            ops.append(HOp("INSPECT", "INSPECT_CMM", 10, 2.5 + 0.5 * qn(tight)))
        else:
            ops.append(HOp("INSPECT", "INSPECT", 0, 0.8 + 0.12 * n_feat))
        kg = weight_kg(finished_volume_cm3(spec), material.density_g_cm3) if material else 1.0
        ops.append(HOp("WASH_PACK", "WASH_PACK", 0, 0.2 + 0.03 * kg))

    part_noise = lat.get("noise") or rng.lognormvariate(0, PART_SIGMA)
    bias = FAMILY_BIAS[fam]
    out = []
    for o in ops:
        if o.outsourced:
            out.append(o)
            continue
        k = bias * part_noise * rng.lognormvariate(0, OP_SIGMA)
        out.append(HOp(o.op_code, o.work_center, o.setup * k + CHANGEOVER_MIN, o.cycle * k))
    return out


def actual_ops(hops: list[HOp], *, qty: int, plant: str) -> list[RoutingOp]:
    """Hidden ops → RoutingOps as they would have run for one batch at one plant."""
    crew = CREW_FACTOR[plant]
    return [
        RoutingOp(
            seq=10 * (i + 1),
            op_code=o.op_code,
            work_center=o.work_center,
            description=f"{o.op_code} (actual)",
            setup_min=round(o.setup * crew, 2) if not o.outsourced else 0.0,
            cycle_min=round(o.cycle * learning(qty) * crew, 3) if not o.outsourced else 0.0,
            outsourced=o.outsourced,
            service_code=o.service_code,
            basis="manual",
            rule_ids=[],
            confidence=1.0,
        )
        for i, o in enumerate(hops)
    ]


# =====================================================================================================
# 3. Part catalogue: anchors + random families
# =====================================================================================================


def anchors(rng: random.Random) -> list[Draft]:
    """Hand-placed parts the demo RFQs rely on (fixed noise so the demo is reproducible and explainable)."""
    return [
        # (a) shaft near SH-4711 (Ø40×220, 42CrMo4+QT, k6 seats, keyway): similar, not reuse-level
        make_shaft(
            rng,
            "SH-4650",
            "Output Shaft",
            "42CrMo4",
            38,
            195,
            seat_fit="k6",
            seat_ra=0.8,
            journal_fit="h6",
            keyways=1,
            center_threads=2,
            radial_threads=2,
            cross_holes=2,
            extra_seat=("js5", 0.4),
            ht="QT 28-32 HRC",
            surface="black oxide",
            material_text="42CrMo4+QT",
            latent={"noise": 1.15},
        ),
        # (b) predecessor of FL-2208: same material, Ø118 vs Ø120, same features → REUSE-001
        make_flange(
            rng,
            "FL-2150",
            "Motor Flange",
            "AW6082",
            118,
            25,
            recess=80,
            bore=52,
            n_threads=4,
            thread_m=6,
            n_holes=4,
            hole_d=9,
            pcd=100,
            face_tol=0.1,
            surface="anodized black",
            latent={"noise": 1.0},
        ),
        # (c) internal parts referenced by ASM-5100
        make_block(
            rng,
            "HS-5101",
            "Actuator Housing",
            "GJS500",
            (75, 90, 90),
            family="housing",
            bores=[(47, 75, "H7", 1.6), (35, 8, "H8", None)],
            holes=[(4, 9, 20, None)],
            threads=[(4, 5, 12)],
            flat_face=0.05,
            latent={"clampings": 3},
        ),
        make_shaft(
            rng,
            "SH-5102",
            "Actuator Shaft",
            "C45",
            25,
            130,
            seat_fit="k6",
            seat_ra=0.8,
            keyways=1,
            grooves=1,
            center_threads=0,
        ),
        make_flange(
            rng,
            "EC-5053",
            "End Cover",
            "AW6082",
            90,
            12,
            recess=None,
            bore=22,
            n_threads=0,
            n_holes=4,
            hole_d=5.5,
            pcd=76,
            face_tol=None,
            surface="anodized natural",
            family="flange",
        ),
        # (d) bracket loosely similar to BR-0930 (160×80×60, pocket, thin wall, deep hole, 4×M8, 2×Ø8 H7)
        make_block(
            rng,
            "BR-0870",
            "Sensor Bracket",
            "AW6082",
            (150, 70, 50),
            family="bracket",
            pockets=[(50, 100, 30)],
            wall=4.0,
            holes=[(2, 8, 12, "H7"), (2, 6.6, 50, None)],
            threads=[(4, 6, 12)],
            surface="anodized natural",
            latent={"clampings": 3},
        ),
    ]


# Product lines: each family is a few lines of variants (fixed material / feature pattern, varying size).
# That is how a real part history looks, and it is what makes similar-part calibration informative.


def _size(rng: random.Random, sizes: list[float], i: int) -> float:
    return float(sizes[i % len(sizes)])


def line_shaft(rng: random.Random, pn: str, line: str, i: int) -> Draft:
    if line == "spindle":  # same line as anchor SH-4650
        d = _size(rng, [25, 30, 50, 55], i)
        return make_shaft(
            rng,
            pn,
            "Spindle Shaft",
            "42CrMo4",
            d,
            5 * round(d * rng.uniform(4.8, 5.6) / 5),
            seat_fit="k6",
            seat_ra=0.8,
            journal_fit="h6",
            keyways=1,
            center_threads=2,
            radial_threads=2,
            cross_holes=2,
            extra_seat=("js5", 0.4),
            ht="QT 28-32 HRC",
            surface="black oxide",
        )
    if line == "idler":
        d = _size(rng, [20, 25, 30, 35, 22, 28, 32], i)
        return make_shaft(
            rng,
            pn,
            "Idler Shaft",
            "C45",
            d,
            5 * round(d * rng.uniform(5, 7) / 5),
            seat_fit=rng.choice(["k6", "h6"]),
            seat_ra=0.8,
            journal_fit="h7",
            keyways=0,
            grooves=2,
            center_threads=0,
            cross_holes=rng.choice([0, 1]),
            surface=rng.choice(["zinc plated", None]),
        )
    if line == "pinion":
        d = _size(rng, [55, 60, 65, 70, 60], i)
        return make_shaft(
            rng,
            pn,
            "Pinion Shaft",
            "16MnCr5",
            d,
            5 * round(d * rng.uniform(4.5, 5.5) / 5),
            seat_fit="k6",
            seat_ra=0.8,
            journal_fit="k6",
            keyways=1,
            center_threads=2,
            pinion=(rng.choice([2.5, 3.0]), 0.5 * d),
            ht="case hardened 58-62 HRC",
        )
    d = _size(rng, [18, 20, 22, 25, 16, 24, 20], i)  # pump shafts
    return make_shaft(
        rng,
        pn,
        "Pump Shaft",
        "X5CrNi18-10",
        d,
        5 * round(d * rng.uniform(6, 8) / 5),
        seat_fit="h7",
        seat_ra=1.6,
        journal_fit="h7",
        keyways=1,
        center_threads=1,
    )


def line_flange(rng: random.Random, pn: str, line: str, i: int) -> Draft:
    if line == "motor":  # same line as anchor FL-2150
        d = _size(rng, [80, 90, 100, 140, 150, 160, 180], i)
        return make_flange(
            rng,
            pn,
            "Motor Flange",
            "AW6082",
            d,
            5 * round(0.2 * d / 5),
            recess=5 * round(0.67 * d / 5),
            bore=2 * round(0.43 * d / 2),
            n_threads=4,
            thread_m=6 if d < 125 else 8,
            n_holes=4,
            hole_d=9 if d < 125 else 11,
            face_tol=0.1,
            surface=rng.choice(["anodized black", "anodized natural"]),
        )
    if line == "bearing_cover":
        d = _size(rng, [72, 80, 90, 110, 125], i)
        return make_flange(
            rng,
            pn,
            "Bearing Cover",
            "C45",
            d,
            rng.choice([10.0, 12.0]),
            recess=5 * round(0.55 * d / 5),
            recess_fit="H8",
            bore=None,
            n_threads=0,
            n_holes=rng.choice([4, 6]),
            hole_d=6.6 if d < 100 else 9.0,
            face_tol=None,
            surface="zinc plated",
        )
    if line == "cast_cover":
        d = _size(rng, [140, 160, 180], i)
        return make_flange(
            rng,
            pn,
            "Housing Cover",
            "GJS500",
            d,
            rng.choice([18.0, 20.0, 22.0]),
            recess=5 * round(0.7 * d / 5),
            bore=5 * round(0.3 * d / 5),
            n_threads=0,
            n_holes=6,
            hole_d=11.0,
            face_tol=0.1,
        )
    d = _size(rng, [80, 100, 120], i)  # stainless covers
    return make_flange(
        rng,
        pn,
        "Sealing Cover",
        "X5CrNi18-10",
        d,
        rng.choice([10.0, 12.0]),
        recess=None,
        bore=5 * round(0.3 * d / 5),
        n_threads=4,
        thread_m=5,
        n_holes=4,
        hole_d=6.6,
        face_tol=0.05,
    )


def line_bracket(rng: random.Random, pn: str, line: str, i: int) -> Draft:
    if line == "sensor":  # same line as anchor BR-0870
        ln = _size(rng, [100, 120, 180, 200, 220], i)
        w, h = 5 * round(0.47 * ln / 5), 5 * round(0.33 * ln / 5)
        return make_block(
            rng,
            pn,
            "Sensor Bracket",
            "AW6082",
            (ln, w, h),
            family="bracket",
            pockets=[(5 * round(0.7 * w / 5), 5 * round(0.65 * ln / 5), round(0.6 * h, 1))],
            wall=4.0,
            holes=[(2, 8, 12, "H7"), (2, 6.6, h, None)],
            threads=[(4, 6, 12)],
            surface="anodized natural",
            latent={"clampings": 3},
        )
    if line == "angle":
        ln = _size(rng, [80, 100, 120, 150, 180], i)
        return make_block(
            rng,
            pn,
            "Angle Bracket",
            "C45",
            (ln, 5 * round(0.5 * ln / 5), 5 * round(0.25 * ln / 5)),
            family="bracket",
            holes=[(4, 9.0, 12.0, None)],
            threads=[(2, 8, 16)],
            slots=2,
            surface=rng.choice(["zinc plated", "black oxide"]),
            latent={"clampings": 2},
        )
    ln = _size(rng, [60, 70, 80, 90], i)  # stainless clamp blocks
    return make_block(
        rng,
        pn,
        "Clamp Block",
        "X5CrNi18-10",
        (ln, 5 * round(0.6 * ln / 5), 5 * round(0.5 * ln / 5)),
        family="bracket",
        holes=[(2, 6.6, 30.0, None), (2, 6.0, 10.0, "H7")],
        threads=[(4, 6, 12)],
        flat_face=0.05,
        latent={"clampings": rng.choice([3, 4])},
    )


def line_housing(rng: random.Random, pn: str, line: str, i: int) -> Draft:
    bearing = _size(rng, [52, 62, 72, 47, 62, 52], i)
    side = 5 * round((bearing + 45) / 5)
    ln = 5 * round(rng.uniform(1.4, 1.8) * bearing / 5)
    if line == "cast":  # same line as HS-5101
        return make_block(
            rng,
            pn,
            "Bearing Housing",
            "GJS500",
            (ln, side, side),
            family="housing",
            bores=[(bearing, ln, "H7", 1.6), (bearing - 12, 8.0, "H8", None)],
            holes=[(4, 9.0, 20.0, None)],
            threads=[(4, 5 if bearing < 60 else 6, 12.0)],
            flat_face=0.05,
            latent={"clampings": 3},
        )
    return make_block(
        rng,
        pn,
        "Actuator Housing",
        "AW6082",
        (ln, side, side),
        family="housing",
        pockets=[(bearing - 10, ln - 20, round(0.5 * side, 1))],
        bores=[(bearing, ln, "H7", 0.8)],
        holes=[(4, 9.0, 20.0, None)],
        threads=[(6, 6, 12.0)],
        flat_face=0.02,
        surface="anodized natural",
        latent={"clampings": 4},
    )


def line_gear(rng: random.Random, pn: str, line: str, i: int) -> Draft:
    if line == "case":
        m = rng.choice([2.0, 2.5])
        z = int(_size(rng, [30, 34, 40, 44, 50, 36, 46, 38], i))
        return make_gear(
            rng,
            pn,
            "16MnCr5",
            m,
            z,
            20.0 if m == 2 else 25.0,
            bore=5 * round(0.3 * m * z / 5),
            bore_ra=0.8,
            ht="case hardened 58-62 HRC",
        )
    z = int(_size(rng, [40, 48, 56, 44], i))
    return make_gear(
        rng, pn, "42CrMo4", 3.0, z, 30.0, bore=5 * round(0.3 * 3 * z / 5), bore_ra=1.6, ht="QT 28-32 HRC"
    )


# family → (maker, part-number format, first number, [(line, count)])
LINES = {
    "shaft": (line_shaft, "SH-4{:03d}", 101, [("spindle", 4), ("idler", 7), ("pinion", 5), ("pump", 7)]),
    "flange": (
        line_flange,
        "FL-2{:03d}",
        301,
        [("motor", 7), ("bearing_cover", 5), ("cast_cover", 3), ("stainless_cover", 3)],
    ),
    "bracket": (line_bracket, "BR-0{:03d}", 501, [("sensor", 5), ("angle", 5), ("clamp", 4)]),
    "housing": (line_housing, "HS-5{:03d}", 201, [("cast", 6), ("alu", 3)]),
    "gear": (line_gear, "GR-3{:03d}", 401, [("case", 8), ("qt", 4)]),
}


def assemblies(rng: random.Random, drafts: list[Draft]) -> list[Draft]:
    """8 assemblies built from historical internal parts + catalog parts."""
    by_fam: dict[str, list[str]] = {}
    for d in drafts:
        by_fam.setdefault(d.family, []).append(d.part_number)
    screws = [("M5x16", "ISO4762-M5x16"), ("M6x16", "ISO4762-M6x16"), ("M6x20", "ISO4762-M6x20")]
    out = [
        make_assembly(
            rng,
            "ASM-5050",
            "Actuator Sub-Assembly",
            (90, 120),
            [
                ("HS-5101", "Gehäuse", 1, "EN-GJS-500-7", None),
                ("SH-5102", "Welle", 1, "C45", None),
                ("6204-2RS", "Rillenkugellager 20×47×14", 2, None, "DIN 625"),
                ("SEAL-20x35x7", "Wellendichtring 20×35×7 NBR", 1, None, "DIN 3760"),
                ("ISO4762-M5x16", "Zylinderschraube M5×16-8.8", 4, None, "ISO 4762"),
                ("EC-5053", "Enddeckel", 1, "EN AW-6082 T6", None),
                ("DIN471-20", "Sicherungsring 20×1.2", 1, None, "DIN 471"),
            ],
        )
    ]
    bearings = ["6204-2RS", "6205-2RS", "6206-2RS", "6207-2RS", "6004-2RS"]
    for i in range(7):
        housing = rng.choice(by_fam["housing"])
        shaft = rng.choice(by_fam["shaft"])
        items = [
            (housing, "Housing", 1, None, None),
            (shaft, "Shaft", 1, None, None),
            (rng.choice(bearings), "Deep groove ball bearing", 2, None, "DIN 625"),
        ]
        if rng.random() < 0.6:
            items.append((rng.choice(by_fam["gear"]), "Gear", 1, None, None))
        if rng.random() < 0.7:
            items.append((rng.choice(by_fam["flange"]), "Cover", 1, None, None))
        sc, sp = rng.choice(screws)
        items.append((sp, f"Socket head cap screw {sc}", rng.choice([4, 6, 8]), None, "ISO 4762"))
        if rng.random() < 0.5:
            items.append(("SEAL-25x40x7", "Radial shaft seal 25x40x7", 1, None, "DIN 3760"))
        d = float(rng.choice([90, 110, 130, 150]))
        out.append(
            make_assembly(
                rng,
                f"ASM-{6010 + 10 * i}",
                rng.choice(["Gear Unit", "Drive Module", "Bearing Unit", "Pump Sub-Assembly"]),
                (d, float(rng.choice([120, 150, 180, 220]))),
                items,
            )
        )
    return out


GUARD_MARGIN = 0.02  # a random part must stay this far below the anchor's score vs the demo drawing
FL_GUARD = 0.90  # no random part may come near reuse level vs FL-2208 (only the anchor FL-2150 does)


def _guards(mats: MaterialRepo, anchor_drafts: list[Draft]) -> list[tuple[dict, float]]:
    """(features of a demo drawing, max score a random historical part may reach against it)."""
    from rfq_agent.config import SAMPLES_DIR

    def gold(rfq: str, pn: str) -> DrawingSpec | None:
        p = SAMPLES_DIR / rfq / "expected" / f"{pn}.json"
        return (
            postprocess(DrawingSpec.model_validate_json(p.read_text(encoding="utf-8")), mats)
            if p.exists()
            else None
        )

    by_pn = {d.part_number: d for d in anchor_drafts}
    out = []
    sh = gold("RFQ-2026-0101", "SH-4711")
    if sh is not None:
        f = featurize_spec(sh, mats)
        anchor = featurize_spec(postprocess(by_pn["SH-4650"].spec, mats), mats)
        out.append((f, score_features(f, anchor)[0] - GUARD_MARGIN))
    fl = gold("RFQ-2026-0102", "FL-2208")
    if fl is not None:
        out.append((featurize_spec(fl, mats), FL_GUARD))
    return out


def all_drafts(rng: random.Random, mats: MaterialRepo) -> list[Draft]:
    """Anchors + product lines: shaft ×25, flange/cover ×20, bracket ×15, housing ×10, gear ×12, asm ×8.

    Demo control: a generated variant that would out-rank an anchor against a demo drawing is re-drawn
    (up to 30 times), so the demo's nearest neighbours are the hand-placed anchors.
    """
    drafts = anchors(rng)
    guards = _guards(mats, drafts)
    for fam, (maker, fmt, start, lines) in LINES.items():
        k = 0
        for line, n in lines:
            for i in range(n):
                pn = fmt.format(start + 7 * k)
                k += 1
                for _ in range(30):
                    d = maker(rng, pn, line, i)
                    feats = featurize_spec(postprocess(d.spec, mats), mats)
                    if all(score_features(g, feats)[0] < limit for g, limit in guards):
                        break
                else:
                    raise RuntimeError(f"could not place {pn} ({fam}/{line}) below the demo guards")
                drafts.append(d)
    return drafts + assemblies(rng, drafts)


# =====================================================================================================
# 4. Persist
# =====================================================================================================


def _insert_part(
    conn: sqlite3.Connection, part_id: int, d: Draft, spec: DrawingSpec, mats: MaterialRepo
) -> None:
    cols = part_columns(spec, mats)
    m = mats.all.get(cols["material_code"] or "")
    fin_kg = round(weight_kg(finished_volume_cm3(spec), m.density_g_cm3), 3) if m else None
    row = {
        "part_id": part_id,
        "part_number": d.part_number,
        "title": spec.title_block.title,
        "family": d.family,
        **cols,
        "finished_weight_kg": fin_kg,
        "spec_json": spec.model_dump_json(),
        "created_at": spec.title_block.date,
    }
    conn.execute(
        f"INSERT INTO parts ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values())
    )


def build_history(conn: sqlite3.Connection, *, write_specs: bool = True) -> dict[str, int]:
    """Generate and persist the full history into `conn` (master data must be loaded). Deterministic."""
    rng = random.Random(SEED)
    for t in ("quote_history", "assembly_bom", "part_costs", "routings", "parts"):
        conn.execute(f"DELETE FROM {t}")
    mats = MaterialRepo(conn)
    rates, services = RateRepo(conn), ServiceRepo(conn).all
    catalog, parts = CatalogRepo(conn), PartRepo(conn)

    drafts = all_drafts(rng, mats)
    lines = []
    n_costs = n_quotes = n_bom = 0
    for part_id, d in enumerate(drafts, start=1):
        spec = postprocess(d.spec, mats)
        d.spec = spec
        _insert_part(conn, part_id, d, spec, mats)
        material = mats.all.get(spec.title_block.material_code or "")
        hops = hidden_routing(d, material, rng)
        mes = actual_ops(hops, qty=MES_REF_QTY, plant="DE")
        conn.executemany(
            "INSERT INTO routings (part_id, seq, op_code, work_center, setup_min, cycle_min, service_code) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (part_id, o.seq, o.op_code, o.work_center, o.setup_min, o.cycle_min, o.service_code)
                for o in mes
            ],
        )
        if spec.envelope.shape_class == ShapeClass.ASSEMBLY:
            conn.executemany(
                "INSERT INTO assembly_bom VALUES (?,?,?,?)",
                [
                    (part_id, it.item_no, it.part_number or it.description, it.quantity)
                    for it in spec.parts_list
                ],
            )
            n_bom += len(spec.parts_list)
        fin_kg = weight_kg(finished_volume_cm3(spec), material.density_g_cm3) if material else 0.0
        bom = build_bom(spec, material, mes, catalog, parts)
        costs: dict[tuple[str, int], float] = {}
        for plant in PLANTS:
            for q in QTYS:
                routing = actual_ops(hops, qty=q, plant=plant)
                res = compute_costs(
                    routing=routing,
                    bom=bom,
                    quantities=[q],
                    finished_weight_kg=fin_kg,
                    requested_delivery=None,
                    today=COST_DATE,
                    rates=rates,
                    services=services,
                )
                cb = next(c for c in res if c.plant == plant)
                if cb.feasible:
                    costs[(plant, q)] = round(cb.unit_cost_eur, 2)
        conn.executemany(
            "INSERT INTO part_costs VALUES (?,?,?,?)", [(part_id, p, q, c) for (p, q), c in costs.items()]
        )
        n_costs += len(costs)
        for _ in range(rng.randint(1, 3)):
            (plant, q), cost = rng.choice(sorted(costs.items()))
            margin = rng.uniform(0.12, 0.25)
            won = rng.random() < max(0.1, 0.85 - 3.0 * (margin - 0.12))
            day = date.fromisoformat(spec.title_block.date) + timedelta(days=rng.randrange(20, 400))
            conn.execute(
                "INSERT INTO quote_history (part_id, customer, plant, qty, unit_price_eur, quoted_at, won) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    part_id,
                    rng.choice(CUSTOMERS),
                    plant,
                    q,
                    round(cost / (1 - margin), 2),
                    day.isoformat(),
                    int(won),
                ),
            )
            n_quotes += 1
        conn.commit()  # children must be visible (costed) before assemblies are built
        lines.append(
            json.dumps(
                {"part_id": part_id, "part_number": d.part_number, "spec": spec.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    if write_specs:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        SPECS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"parts": len(drafts), "part_costs": n_costs, "quote_history": n_quotes, "assembly_bom": n_bom}


def load_specs(path: Path = SPECS_FILE) -> list[dict]:
    """Read data/history/specs.jsonl → [{part_id, part_number, spec: DrawingSpec}]."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            rec["spec"] = DrawingSpec.model_validate(rec["spec"])
            out.append(rec)
    return out


if __name__ == "__main__":
    from rfq_agent.config import DB_PATH
    from rfq_agent.data.db import connect

    print(build_history(connect(DB_PATH)))
