"""Process planning: rule engine R01–R14 + similar-part calibration (DESIGN §5.7)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from rfq_agent.agents.geometry import (
    envelope_volume_cm3,
    feature_it_grade,
    finished_volume_cm3,
    main_dims,
    select_stock,
)
from rfq_agent.config import BIZ
from rfq_agent.data.repositories import Material, PartRepo
from rfq_agent.models import DrawingSpec, Feature, FeatureType, RoutingOp, ShapeClass, SimilarPart
from rfq_agent.models.drawing import Envelope

OUTSOURCED_WC = "OUTSOURCED"  # pseudo work centre for external ops; costing prices them by service_code

# Fixed operation order template (op_code → rank)
OP_ORDER = [
    "SAW",
    "TURN",
    "MILL",
    "DRILL",
    "KEYWAY",
    "HOB",
    "HEAT_TREAT",
    "GRIND",
    "SURFACE",
    "DEBURR",
    "INSPECT",
    "ASSEMBLY",
    "TEST",
    "WASH_PACK",
]
# Used to place reference-only (suggested) ops whose op_code is unknown to the template
_WC_RANK = {
    "SAW": "SAW",
    "CNC_TURN": "TURN",
    "CNC_MILL_3AX": "MILL",
    "CNC_MILL_5AX": "MILL",
    "DRILL": "DRILL",
    "HOB": "HOB",
    "GRIND_CYL": "GRIND",
    "GRIND_SURF": "GRIND",
    "DEBURR": "DEBURR",
    "INSPECT": "INSPECT",
    "INSPECT_CMM": "INSPECT",
    "ASSEMBLY": "ASSEMBLY",
    "TEST": "TEST",
    "WASH_PACK": "WASH_PACK",
}
_SERVICE_RANK = {"HT_QT": "HEAT_TREAT", "HT_CASE": "HEAT_TREAT"}

RULE_CONFIDENCE = 0.70
FASTENER_RE = re.compile(
    r"\b(iso\s*(4762|4017|4014|4032|7089|7991)|din\s*(912|933|934|125|471|472|6885|7991)"
    r"|screws?|bolts?|nuts?|washers?|retaining ring|circlip|parallel key|dowel|pins?"
    r"|schrauben?|muttern?|scheiben?|sicherungsring|passfeder)\b"
)


@dataclass
class Ctx:
    """Everything the rules need, derived once from the spec."""

    spec: DrawingSpec
    material: Material | None
    services: dict
    shape: ShapeClass
    m: float  # machinability factor
    v_removed: float  # cm³
    stock_cut_mm: float
    covered: set[str] = field(default_factory=set)
    uncovered_extra: list[str] = field(default_factory=list)

    def of(self, *types: FeatureType) -> list[Feature]:
        return [f for f in self.spec.features if f.type in types]

    @staticmethod
    def n(feats: list[Feature]) -> int:
        return sum(f.quantity for f in feats)

    def cover(self, feats: list[Feature]) -> None:
        self.covered.update(f.id for f in feats)


def _op(
    op_code: str, wc: str, desc: str, setup: float, cycle: float, rule: str, conf=RULE_CONFIDENCE, **kw
) -> RoutingOp:
    return RoutingOp(
        seq=0,
        op_code=op_code,
        work_center=wc,
        description=desc,
        setup_min=round(setup, 2),
        cycle_min=round(cycle, 2),
        basis="rule",
        rule_ids=[rule],
        rule_cycle_min=round(cycle, 2),
        confidence=conf,
        **kw,
    )


def _text(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def _is_internal_thread(f: Feature, max_d: float) -> bool:
    t = _text(f.description, f.evidence.text if f.evidence else None)
    if any(k in t for k in ("internal", "tapped", "innengewinde", "center hole", "centre hole", "zentrier")):
        return True
    if any(k in t for k in ("external", "außengewinde", "aussengewinde")):
        return False
    return not (f.nominal_mm and max_d and f.nominal_mm >= 0.5 * max_d)


def _precision(f: Feature) -> tuple[int | None, float | None]:
    return feature_it_grade(f.nominal_mm, f.tolerance), f.ra_um


# ---------- rules ----------


def r01_saw(c: Ctx) -> list[RoutingOp]:
    """R01 every make part: saw from bar / plate."""
    return [_op("SAW", "SAW", "Saw raw stock to length", 5, 0.5 + 0.02 * c.stock_cut_mm, "R01")]


def r02_turn(c: Ctx) -> list[RoutingOp]:
    """R02 rotational: CNC turning incl. OD, bores, grooves, chamfers, faces, external threads."""
    if c.shape != ShapeClass.ROTATIONAL:
        return []
    max_d = main_dims(c.spec)[0]
    ods = c.of(FeatureType.OUTER_DIAMETER)
    grooves = c.of(FeatureType.GROOVE)
    chamfers = c.of(FeatureType.CHAMFER)
    ext_threads = [f for f in c.of(FeatureType.THREAD) if not _is_internal_thread(f, max_d)]
    c.cover(
        ods + grooves + chamfers + ext_threads + c.of(FeatureType.BORE, FeatureType.FACE, FeatureType.WALL)
    )
    n_od = c.n(ods)
    two_sided = n_od >= 3 or (bool(c.of(FeatureType.BORE)) and n_od >= 2)
    setup = 30 + (10 if two_sided else 0)
    cycle = (
        1.0
        + 0.015 * c.v_removed / c.m
        + 0.3 * n_od
        + 0.4 * (c.n(grooves) + c.n(ext_threads))
        + 0.1 * c.n(chamfers)
    )
    desc = "CNC turning" + (" (both ends)" if two_sided else "")
    return [_op("TURN", "CNC_TURN", desc, setup, cycle, "R02")]


def _hole_cycle(holes: list[Feature], threads: list[Feature]) -> float:
    t = 0.0
    for f in holes:
        ratio = (f.length_mm / f.nominal_mm) if (f.length_mm and f.nominal_mm) else 2.0
        t += 0.3 * f.quantity * (1 + ratio / 5)
    return t + 0.4 * sum(f.quantity for f in threads)


def r03_mill(c: Ctx) -> list[RoutingOp]:
    """R03 prismatic: 3-axis milling (5-axis if noted); R04 holes/threads merged into this op."""
    if c.shape != ShapeClass.PRISMATIC:
        return []
    pockets = c.of(FeatureType.POCKET)
    slots = c.of(FeatureType.SLOT, FeatureType.GROOVE, FeatureType.KEYWAY)
    holes = c.of(FeatureType.HOLE, FeatureType.BORE)
    threads = c.of(FeatureType.THREAD)
    c.cover(
        pockets
        + slots
        + holes
        + threads
        + c.of(FeatureType.FACE, FeatureType.WALL, FeatureType.CHAMFER, FeatureType.OUTER_DIAMETER)
    )
    txt = _text(*c.spec.notes, *(f.description for f in c.spec.features))
    five = any(k in txt for k in ("5-axis", "5 axis", "5-achs", "5 achs", "simultaneous"))
    cycle = 2.0 + 0.02 * c.v_removed / c.m + 0.5 * c.n(pockets) + 0.3 * c.n(slots)
    rules = "R03"
    op = _op(
        "MILL",
        "CNC_MILL_5AX" if five else "CNC_MILL_3AX",
        "CNC milling " + ("5-axis" if five else "3-axis"),
        45,
        cycle,
        rules,
    )
    if holes or threads:
        extra = _hole_cycle(holes, threads)
        op.cycle_min = op.rule_cycle_min = round(cycle + extra, 2)
        op.rule_ids.append("R04")
        op.description += " incl. drilling / tapping"
    return [op]


def r04_drill(c: Ctx) -> list[RoutingOp]:
    """R04 rotational: separate drilling / tapping op for cross/axial holes and internal threads."""
    if c.shape != ShapeClass.ROTATIONAL:
        return []
    max_d = main_dims(c.spec)[0]
    holes = c.of(FeatureType.HOLE)
    threads = [f for f in c.of(FeatureType.THREAD) if _is_internal_thread(f, max_d)]
    if not holes and not threads:
        return []
    c.cover(holes + threads)
    return [_op("DRILL", "DRILL", "Drilling / tapping", 15, _hole_cycle(holes, threads), "R04")]


def r05_keyway(c: Ctx) -> list[RoutingOp]:
    """R05 rotational keyways / slots milled on the 3-axis mill."""
    if c.shape != ShapeClass.ROTATIONAL:
        return []
    kw = c.of(FeatureType.KEYWAY, FeatureType.SLOT)
    if not kw:
        return []
    c.cover(kw)
    return [_op("KEYWAY", "CNC_MILL_3AX", "Mill keyway", 20, 1.5 * c.n(kw), "R05")]


def r06_hob(c: Ctx) -> list[RoutingOp]:
    """R06 gear teeth: hobbing."""
    gears = c.of(FeatureType.GEAR_TEETH)
    if not gears:
        return []
    cycle = 0.0
    conf = RULE_CONFIDENCE
    for g in gears:
        z = g.gear_teeth or 20
        b = g.face_width_mm or g.length_mm or 20.0
        mod = g.gear_module or 2.0
        if not (g.gear_teeth and g.gear_module):
            conf = 0.5
        cycle += 0.12 * z * b / 10 * math.sqrt(mod) * g.quantity
    c.cover(gears)
    return [_op("HOB", "HOB", "Gear hobbing", 40, cycle, "R06", conf)]


HT_KEYWORDS = {
    "HT_CASE": ("case", "carburi", "einsatz", "aufgekohlt", "eht"),
    "HT_QT": ("qt", "quench", "temper", "vergüt", "verguet", "+q"),
}
SURFACE_KEYWORDS = {
    "ANODIZE": ("anodi", "eloxal", "eloxier"),
    "ZINC": ("zinc", "zink", "galvani", "verzinkt"),
    "BLACK": ("black oxide", "blacken", "brüniert", "bruniert", "brueniert", "schwarz", "black"),
}


def service_for(text: str | None, table: dict[str, tuple[str, ...]]) -> str | None:
    t = _text(text)
    if not t:
        return None
    for code, keys in table.items():
        if any(k in t for k in keys):
            return code
    return None


def _outsourced(op_code: str, code: str, c: Ctx, rule: str, label: str) -> RoutingOp:
    name = (c.services.get(code) or {}).get("name", code)
    return _op(
        op_code,
        OUTSOURCED_WC,
        f"{name} (outsourced, {label})",
        0,
        0,
        rule,
        0.8,
        outsourced=True,
        service_code=code,
    )


def r07_heat_treat(c: Ctx) -> list[RoutingOp]:
    """R07 QT / case hardening → outsourced heat treatment."""
    ht = c.spec.heat_treatment
    if not ht:
        return []
    code = service_for(ht, HT_KEYWORDS)
    conf = 0.8
    if code is None and any(k in _text(ht) for k in ("harden", "hrc", "gehärtet", "gehaertet")):
        # generic "hardened xx HRC": case-hardening steels are carburised, others through-hardened
        grp = c.material.mat_group if c.material else None
        code, conf = ("HT_CASE" if grp == "case_hardening_steel" else "HT_QT"), 0.5
    if code is None:
        c.uncovered_extra.append("heat_treatment")
        return []
    op = _outsourced("HEAT_TREAT", code, c, "R07", ht)
    op.confidence = conf
    return [op]


def r08_grind(c: Ctx) -> list[RoutingOp]:
    """R08 any feature ≤ IT6 or Ra ≤ 0.8 → cylindrical / surface grinding."""
    ground = []
    for f in c.spec.features:
        it, ra = _precision(f)
        if (it is not None and it <= 6) or (ra is not None and ra <= 0.8):
            ground.append(f)
    if not ground:
        return []
    n = c.n(ground)
    length = sum((f.length_mm or 20.0) * f.quantity for f in ground)
    if c.shape == ShapeClass.ROTATIONAL:
        wc, desc = "GRIND_CYL", "Cylindrical grinding"
    else:
        wc, desc = "GRIND_SURF", "Surface grinding"
    ids = ", ".join(f.id for f in ground)
    return [_op("GRIND", wc, f"{desc} ({ids})", 25, 1.5 * n + 0.02 * length, "R08")]


def r09_surface(c: Ctx) -> list[RoutingOp]:
    """R09 anodizing / zinc / black oxide → outsourced surface treatment."""
    st = c.spec.surface_treatment
    if not st:
        return []
    code = service_for(st, SURFACE_KEYWORDS)
    if code is None:
        c.uncovered_extra.append("surface_treatment")
        return []
    conf = 0.8
    grp = c.material.mat_group if c.material else None
    if code == "ANODIZE" and grp and grp != "aluminium":
        conf = 0.4  # anodizing on non-aluminium — review
    op = _outsourced("SURFACE", code, c, "R09", st)
    op.confidence = conf
    return [op]


def r10_deburr(c: Ctx) -> list[RoutingOp]:
    """R10 every make part: deburring."""
    return [_op("DEBURR", "DEBURR", "Deburring", 0, 0.5 + 0.05 * c.n(c.spec.features), "R10")]


def r11_inspect(c: Ctx) -> list[RoutingOp]:
    """R11 ≥ 3 features ≤ IT7 → CMM, else manual inspection."""
    tight = [f for f in c.spec.features if (_precision(f)[0] or 99) <= 7]
    if c.n(tight) >= 3:
        return [_op("INSPECT", "INSPECT_CMM", "CMM inspection", 15, 4.0, "R11")]
    return [_op("INSPECT", "INSPECT", "Manual inspection", 0, 1.5, "R11")]


def _is_fastener(item) -> bool:
    return bool(FASTENER_RE.search(_text(item.description, item.standard, item.part_number)))


def r12_assembly(c: Ctx) -> list[RoutingOp]:
    """R12 assembly: 1.2 min per part + 0.3 min per fastener."""
    items = c.spec.parts_list
    n_fast = sum(i.quantity for i in items if _is_fastener(i))
    n_parts = sum(i.quantity for i in items) - n_fast
    c.cover(c.spec.features)  # assembly-level dimensions are checked, not machined
    conf = RULE_CONFIDENCE if items else 0.4
    return [_op("ASSEMBLY", "ASSEMBLY", "Assembly", 20, 1.2 * n_parts + 0.3 * n_fast, "R12", conf)]


def r13_test(c: Ctx) -> list[RoutingOp]:
    """R13 assembly: functional test."""
    return [_op("TEST", "TEST", "Functional test", 10, 5.0, "R13")]


def r14_wash_pack(c: Ctx) -> list[RoutingOp]:
    """R14 everything: washing & packing."""
    return [_op("WASH_PACK", "WASH_PACK", "Washing & packing", 0, 0.3, "R14")]


Rule = Callable[[Ctx], list[RoutingOp]]
PART_RULES: list[tuple[str, Rule]] = [
    ("R01", r01_saw),
    ("R02", r02_turn),
    ("R03", r03_mill),
    ("R04", r04_drill),
    ("R05", r05_keyway),
    ("R06", r06_hob),
    ("R07", r07_heat_treat),
    ("R08", r08_grind),
    ("R09", r09_surface),
    ("R10", r10_deburr),
    ("R11", r11_inspect),
    ("R14", r14_wash_pack),
]
ASSEMBLY_RULES: list[tuple[str, Rule]] = [
    ("R12", r12_assembly),
    ("R13", r13_test),
    ("R14", r14_wash_pack),
]
RULES = {rid: fn for rid, fn in PART_RULES + ASSEMBLY_RULES}


def _rank(op: RoutingOp) -> int:
    key = op.op_code if op.op_code in OP_ORDER else None
    key = key or _SERVICE_RANK.get(op.service_code or "") or _WC_RANK.get(op.work_center)
    if key is None:
        key = "SURFACE" if op.outsourced else "DEBURR"
    return OP_ORDER.index(key)


def _sequence(ops: list[RoutingOp]) -> list[RoutingOp]:
    ordered = sorted(enumerate(ops), key=lambda t: (_rank(t[1]), t[0]))
    out = []
    for i, (_, op) in enumerate(ordered, start=1):
        op.seq = 10 * i
        out.append(op)
    return out


def rule_routing(
    spec: DrawingSpec, material: Material | None, services: dict
) -> tuple[list[RoutingOp], list[str]]:
    """Apply R01–R14. Returns (ops in fixed order, uncovered feature ids)."""
    shape = spec.envelope.shape_class
    services = services or {}
    if shape == ShapeClass.ASSEMBLY:
        ctx = Ctx(spec, material, services, shape, 1.0, 0.0, 0.0)
        rules = ASSEMBLY_RULES
    else:
        stock = select_stock(spec)
        cut = stock.diameter_mm or math.sqrt((stock.width_mm or 0) * (stock.thickness_mm or 0))
        v_removed = max(stock.volume_cm3 - finished_volume_cm3(spec), 0.0)
        m = material.machinability if material and material.machinability else 1.0
        ctx = Ctx(spec, material, services, shape, m, v_removed, cut)
        rules = PART_RULES
    ops: list[RoutingOp] = []
    for _, fn in rules:
        ops.extend(fn(ctx))
    if material is None and shape != ShapeClass.ASSEMBLY:
        for op in ops:  # machinability unknown → times less certain
            op.confidence = round(op.confidence * 0.8, 3)
    uncovered = [f.id for f in spec.features if f.id not in ctx.covered] + ctx.uncovered_extra
    return _sequence(ops), uncovered


# ---------- calibration ----------


def _ref_envelope_cm3(row: dict) -> float:
    shape = row.get("shape_class") or ShapeClass.ROTATIONAL.value
    try:
        env = Envelope(
            shape_class=ShapeClass(shape),
            max_diameter_mm=row.get("max_diameter_mm"),
            length_mm=row.get("length_mm"),
            width_mm=row.get("width_mm"),
            height_mm=row.get("height_mm"),
        )
    except ValueError:
        return 0.0
    return envelope_volume_cm3(DrawingSpec(drawing_file="", title_block={}, envelope=env))


def calibrate(
    ops: list[RoutingOp], spec: DrawingSpec, similar: list[SimilarPart], part_repo: PartRepo
) -> list[RoutingOp]:
    """Blend rule cycle times with the Top-1 similar part's routing (score ≥ blend_min_score)."""
    if not similar:
        return ops
    top = max(similar, key=lambda s: s.score)
    if top.score < BIZ.blend_min_score:
        return ops
    ref_ops = part_repo.routing(top.part_id)
    if not ref_ops:
        return ops
    v_new = envelope_volume_cm3(spec)
    try:
        v_ref = _ref_envelope_cm3(part_repo.get(top.part_id))
    except IndexError:
        v_ref = 0.0
    scale = (v_new / v_ref) ** (2 / 3) if v_new > 0 and v_ref > 0 else 1.0
    w = round(top.score**2, 4)

    unused = list(ref_ops)

    def take(op: RoutingOp) -> dict | None:
        if op.outsourced:
            pred = [lambda r: r.get("service_code") == op.service_code]
        else:
            pred = [
                lambda r: r["work_center"] == op.work_center and r["op_code"] == op.op_code,
                lambda r: r["work_center"] == op.work_center and not r.get("service_code"),
            ]
        for p in pred:
            for r in unused:
                if p(r):
                    unused.remove(r)
                    return r
        return None

    out: list[RoutingOp] = []
    for op in ops:
        op = op.model_copy(deep=True)
        ref = take(op)
        if ref is not None and not op.outsourced:
            scaled = (ref["cycle_min"] or 0.0) * scale
            rule_cycle = op.rule_cycle_min if op.rule_cycle_min is not None else op.cycle_min
            op.cycle_min = round(w * scaled + (1 - w) * rule_cycle, 2)
            op.basis = "blend"
            op.ref_part_id = top.part_id
            op.blend_weight = w
            op.confidence = round(min(0.95, op.confidence + 0.25 * w), 3)
        out.append(op)

    # reference ops the rules did not generate → suggestions only (not costed until accepted)
    for r in unused:
        svc = r.get("service_code")
        out.append(
            RoutingOp(
                seq=0,
                op_code=r["op_code"],
                work_center=OUTSOURCED_WC if svc else r["work_center"],
                description=f"Suggested from similar part {top.part_number}: {r['op_code']}",
                setup_min=0.0 if svc else round(r["setup_min"] or 0.0, 2),
                cycle_min=0.0 if svc else round((r["cycle_min"] or 0.0) * scale, 2),
                outsourced=bool(svc),
                service_code=svc,
                basis="blend",
                rule_ids=[],
                ref_part_id=top.part_id,
                blend_weight=1.0,
                suggested=True,
                confidence=round(0.6 * top.score, 3),
            )
        )
    return _sequence(out)


def plan_routing(
    spec: DrawingSpec,
    material: Material | None,
    services: dict,
    similar: list[SimilarPart],
    part_repo: PartRepo,
) -> tuple[list[RoutingOp], list[str]]:
    """Rule routing followed by similar-part calibration."""
    ops, uncovered = rule_routing(spec, material, services)
    return calibrate(ops, spec, similar, part_repo), uncovered
