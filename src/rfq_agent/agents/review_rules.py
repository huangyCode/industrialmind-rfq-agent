"""Validation + DFM rule engine (DESIGN §5.3). Deterministic; every issue links a knowledge-base chunk."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

from rfq_agent.data.repositories import CatalogRepo, MaterialRepo, PartRepo
from rfq_agent.models import (
    DrawingSpec,
    Feature,
    FeatureType,
    RFQItem,
    Severity,
    ShapeClass,
    ValidationIssue,
)

# Rule thresholds (engineering limits, documented in data/knowledge/*.md)
MIN_WALL_MM = {
    "steel": 2.0,
    "case_hardening_steel": 2.0,
    "stainless": 2.0,
    "aluminium": 1.5,
    "cast_iron": 3.0,
}
MIN_WALL_DEFAULT_MM = 2.0  # unknown material: use the steel limit
DEEP_HOLE_RATIO = 10.0
GRIND_IT_MAX = 6
PRECISION_IT_MAX = 7  # "precision" for the heat-treatment sequence rule
PRECISION_RA_MAX = 0.8
FINE_RA_MAX = 0.4
MIN_INTERNAL_RADIUS_MM = 0.5
THREAD_DEPTH_RATIO = 3.0

_FIT_DIGITS = re.compile(r"[A-Za-z]{1,2}\s*(\d{1,2})")
_RADIUS = re.compile(r"\bR\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_THREAD_D = re.compile(r"M\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_SHARP = re.compile(
    r"\bsharp\b|\bno (?:corner )?radi|\bscharfkantig|\bR\s*0\b(?![.,]\d*[1-9])", re.IGNORECASE
)


def _feature_ref(f) -> str:
    """Feature type + the verbatim drawing text; the model's free-text description can contradict a repaired type."""
    kind = f.type.value.replace("_", " ")
    return f'{kind} "{f.evidence.text}"' if f.evidence and f.evidence.text else f"{kind} ({f.description})"


@dataclass
class Ctx:
    line_no: int
    item: RFQItem
    spec: DrawingSpec | None
    materials: MaterialRepo
    parts: PartRepo

    @cached_property
    def material_code(self) -> str | None:
        if self.spec is None:
            return None
        tb = self.spec.title_block
        return tb.material_code or self.materials.resolve(tb.material)

    @cached_property
    def mat_group(self) -> str | None:
        code = self.material_code
        return self.materials.all[code].mat_group if code in self.materials.all else None

    @property
    def is_assembly(self) -> bool:
        return self.spec is not None and self.spec.envelope.shape_class == ShapeClass.ASSEMBLY

    def issue(self, rule: Rule, message: str, field_path: str | None = None) -> ValidationIssue:
        return ValidationIssue(
            code=rule.id,
            severity=rule.severity,
            line_no=self.line_no,
            field_path=field_path,
            message=message,
            suggestion=rule.suggestion,
            kb_ref=rule.kb_ref,
        )


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    description: str
    suggestion: str
    kb_ref: str
    check: Callable[[Ctx, Rule], list[ValidationIssue]]


RULES: list[Rule] = []


def rule(id: str, severity: Severity, description: str, suggestion: str, kb_ref: str):
    def deco(fn: Callable[[Ctx, Rule], list[ValidationIssue]]):
        RULES.append(Rule(id, severity, description, suggestion, kb_ref, fn))
        return fn

    return deco


# ---------- helpers ----------


def feature_it_grade(f: Feature) -> int | None:
    """IT grade from post-processing; fall back to the digit(s) of a fit code like 'k6' or 'H7/g6'."""
    tol = f.tolerance
    if tol is None:
        return None
    if tol.it_grade is not None:
        return tol.it_grade
    if tol.fit:
        grades = [int(d) for d in _FIT_DIGITS.findall(tol.fit)]
        return min(grades) if grades else None
    return None


def _fp(spec: DrawingSpec, f: Feature, attr: str | None = None) -> str:
    i = spec.features.index(f)
    return f"features[{i}]" + (f".{attr}" if attr else "")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _has_tolerance(f: Feature) -> bool:
    t = f.tolerance
    return t is not None and (
        t.upper is not None or t.lower is not None or bool(t.fit) or t.it_grade is not None
    )


def _feature_text(f: Feature) -> str:
    return " ".join(x for x in (f.description, f.evidence.text if f.evidence else None) if x)


# ---------- validation rules ----------


@rule(
    "VAL-010",
    Severity.BLOCKER,
    "RFQ line has no matching drawing",
    "Ask the customer to send the drawing for this line item.",
    "quoting_policy.md#rfq-completeness",
)
def _no_drawing(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is not None:
        return []
    return [
        c.issue(
            r, f"No drawing found for line {c.line_no} (reference '{c.item.drawing_ref}').", "drawing_ref"
        )
    ]


@rule(
    "VAL-001",
    Severity.BLOCKER,
    "Material field is empty",
    "Ask the customer for the material designation and standard (e.g. '42CrMo4+QT EN 10083-3').",
    "material_standards.md#material-callout",
)
def _material_missing(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or c.is_assembly:
        return []
    if (c.spec.title_block.material or "").strip():
        return []
    return [c.issue(r, "The material field in the title block is empty.", "title_block.material")]


@rule(
    "VAL-002",
    Severity.WARNING,
    "Material cannot be mapped to master data",
    "Confirm an equivalent grade with the customer or check purchasing availability and price.",
    "material_standards.md#equivalents",
)
def _material_unmapped(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or c.is_assembly:
        return []
    text = (c.spec.title_block.material or "").strip()
    if not text or c.material_code:
        return []
    return [c.issue(r, f"Material '{text}' is not in the material master data.", "title_block.material")]


@rule(
    "VAL-003",
    Severity.BLOCKER,
    "Quantity is missing",
    "Ask the customer for the order quantities or the annual demand.",
    "quoting_policy.md#rfq-completeness",
)
def _qty_missing(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if any(q > 0 for q in c.item.quantities):
        return []
    return [c.issue(r, f"No quantity given for line {c.line_no}.", "item.quantities")]


@rule(
    "VAL-004",
    Severity.WARNING,
    "No general tolerance stated",
    "Quote on the basis of ISO 2768-m and state this as an assumption in the quote.",
    "tolerance_guide.md#general-tolerances",
)
def _no_general_tol(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or c.is_assembly or (c.spec.title_block.general_tolerance or "").strip():
        return []
    return [
        c.issue(r, "The drawing states no general tolerance (ISO 2768).", "title_block.general_tolerance")
    ]


@rule(
    "VAL-005",
    Severity.INFO,
    "Features without tolerance and no general tolerance",
    "Quote on the basis of ISO 2768-m and state this as an assumption in the quote.",
    "tolerance_guide.md#general-tolerances",
)
def _untoleranced_features(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or c.is_assembly or (c.spec.title_block.general_tolerance or "").strip():
        return []
    ids = [f.id for f in c.spec.features if f.nominal_mm is not None and not _has_tolerance(f)]
    if not ids:
        return []
    return [c.issue(r, f"Features without tolerance: {', '.join(ids)}.", "features")]


@rule(
    "VAL-011",
    Severity.WARNING,
    "Assembly parts list contains a new part number without drawing",
    "List this item as 'price on request' and ask the customer for the part drawing; quote the rest of the assembly.",
    "quoting_policy.md#assemblies",
)
def _new_parts_list_item(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or not c.spec.parts_list:
        return []
    catalog = CatalogRepo(c.parts.conn)
    out = []
    for i, p in enumerate(c.spec.parts_list):
        if catalog.match(p.part_number, p.description, p.standard):
            continue
        if p.part_number and c.parts.by_number(p.part_number):
            continue
        if p.standard:  # a standard part not in our catalog is bought on request, not a new design
            continue
        label = p.part_number or p.description
        out.append(
            c.issue(
                r,
                f"Parts list item {p.item_no} '{label}' is neither a catalog part nor a known internal part "
                "and no drawing is attached.",
                f"parts_list[{i}]",
            )
        )
    return out


# ---------- DFM rules ----------


@rule(
    "DFM-001",
    Severity.WARNING,
    "Thin wall below the minimum for the material group",
    "Increase the wall thickness or confirm the functional need; expect distortion and chatter.",
    "dfm_guidelines.md#thin-walls",
)
def _thin_wall(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    limit = MIN_WALL_MM.get(c.mat_group or "", MIN_WALL_DEFAULT_MM)
    group = c.mat_group or "unknown material, steel limit used"
    return [
        c.issue(
            r, f"{f.id}: wall {f.nominal_mm} mm < {limit} mm minimum ({group}).", _fp(c.spec, f, "nominal_mm")
        )
        for f in c.spec.features
        if f.type == FeatureType.WALL and f.nominal_mm is not None and f.nominal_mm < limit
    ]


@rule(
    "DFM-002",
    Severity.WARNING,
    "Deep hole, depth/diameter > 10",
    "Gun drilling or stepped drilling needed (higher cost); reduce depth or increase diameter if possible.",
    "dfm_guidelines.md#deep-holes",
)
def _deep_hole(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    out = []
    for f in c.spec.features:
        if f.type == FeatureType.HOLE and f.nominal_mm and f.length_mm:
            ratio = f.length_mm / f.nominal_mm
            if ratio > DEEP_HOLE_RATIO:
                msg = f"{f.id}: hole Ø{f.nominal_mm} × {f.length_mm} mm, L/D = {ratio:.1f} > {DEEP_HOLE_RATIO:g}."
                out.append(c.issue(r, msg, _fp(c.spec, f, "length_mm")))
    return out


@rule(
    "DFM-003",
    Severity.INFO,
    "Tolerance IT6 or finer",
    "Grinding required; confirm that the tolerance is functionally necessary.",
    "tolerance_guide.md#it-grades",
)
def _fine_it(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    out = []
    for f in c.spec.features:
        it = feature_it_grade(f)
        if it is not None and it <= GRIND_IT_MAX:
            out.append(c.issue(r, f"{f.id}: {_feature_ref(f)} is IT{it}.", _fp(c.spec, f, "tolerance")))
    return out


@rule(
    "DFM-004",
    Severity.WARNING,
    "Surface roughness Ra ≤ 0.4 µm",
    "Fine grinding, honing or lapping required (high cost); confirm the requirement.",
    "dfm_guidelines.md#surface-finish",
)
def _fine_ra(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    out = [
        c.issue(r, f"{f.id}: Ra {f.ra_um} µm ≤ {FINE_RA_MAX} µm.", _fp(c.spec, f, "ra_um"))
        for f in c.spec.features
        if f.ra_um is not None and f.ra_um <= FINE_RA_MAX
    ]
    d = c.spec.title_block.default_ra_um
    if d is not None and d <= FINE_RA_MAX:
        out.append(
            c.issue(
                r, f"General surface roughness Ra {d} µm ≤ {FINE_RA_MAX} µm.", "title_block.default_ra_um"
            )
        )
    return out


@rule(
    "DFM-005",
    Severity.INFO,
    "Heat treatment combined with precision tolerances",
    "Sequence: rough machining → heat treatment → grinding; leave grinding allowance.",
    "dfm_guidelines.md#heat-treatment-sequence",
)
def _ht_sequence(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None or not (c.spec.heat_treatment or "").strip():
        return []
    precise = [
        f.id
        for f in c.spec.features
        if ((it := feature_it_grade(f)) is not None and it <= PRECISION_IT_MAX)
        or (f.ra_um is not None and f.ra_um <= PRECISION_RA_MAX)
    ]
    if not precise:
        return []
    return [
        c.issue(
            r,
            f"Heat treatment '{c.spec.heat_treatment}' with precision features {', '.join(precise)}.",
            "heat_treatment",
        )
    ]


def _corner_radius(f: Feature) -> float | None:
    """Stated internal corner radius; 0.0 for 'sharp'; None if not stated."""
    text = _feature_text(f)
    if _SHARP.search(text):
        return 0.0
    m = _RADIUS.search(text)
    return _num(m.group(1)) if m else None


@rule(
    "DFM-006",
    Severity.WARNING,
    "Milled internal corner without radius or radius < 0.5 mm",
    "Specify an internal radius ≥ cutter radius (≥ 0.5 mm, preferably ≥ 1/3 of pocket depth).",
    "dfm_guidelines.md#internal-corners",
)
def _internal_corners(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    out = []
    for f in c.spec.features:
        if f.type not in (FeatureType.POCKET, FeatureType.SLOT):
            continue
        rad = _corner_radius(f)
        if rad is not None and rad < MIN_INTERNAL_RADIUS_MM:
            what = "sharp internal corners" if rad == 0 else f"internal radius R{rad:g}"
            out.append(c.issue(r, f"{f.id}: {_feature_ref(f)} has {what}.", _fp(c.spec, f)))
    # corners called out in the notes, e.g. "Internal corners sharp" / "internal radii R0.2"
    for i, note in enumerate(c.spec.notes):
        if not re.search(r"corner|radi|ecke", note, re.IGNORECASE):
            continue
        m = _RADIUS.search(note)
        rad = 0.0 if _SHARP.search(note) else (_num(m.group(1)) if m else None)
        if rad is not None and rad < MIN_INTERNAL_RADIUS_MM:
            out.append(c.issue(r, f"Note: '{note}'.", f"notes[{i}]"))
    return out


def _thread_d(f: Feature) -> float | None:
    if f.nominal_mm:
        return f.nominal_mm
    m = _THREAD_D.search(f.thread_spec or f.description or "")
    return _num(m.group(1)) if m else None


@rule(
    "DFM-007",
    Severity.INFO,
    "Thread depth > 3 × D",
    "Tapping is difficult and adds no strength; limit thread depth to 2 × D.",
    "dfm_guidelines.md#threads",
)
def _deep_thread(c: Ctx, r: Rule) -> list[ValidationIssue]:
    if c.spec is None:
        return []
    out = []
    for f in c.spec.features:
        if f.type != FeatureType.THREAD or not f.length_mm:
            continue
        d = _thread_d(f)
        if d and f.length_mm / d > THREAD_DEPTH_RATIO:
            label = f.thread_spec or f"M{d:g}"
            msg = f"{f.id}: {label} thread depth {f.length_mm} mm = {f.length_mm / d:.1f} × D."
            out.append(c.issue(r, msg, _fp(c.spec, f, "length_mm")))
    return out


# ---------- entry point ----------


def review_line(
    *, line_no: int, item: RFQItem, spec: DrawingSpec | None, materials: MaterialRepo, parts: PartRepo
) -> list[ValidationIssue]:
    """Run every rule in RULES; issues sorted blocker → warning → info, then by rule order."""
    ctx = Ctx(line_no=line_no, item=item, spec=spec, materials=materials, parts=parts)
    issues = [i for r in RULES for i in r.check(ctx, r)]
    rank = {Severity.BLOCKER: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return sorted(issues, key=lambda i: rank[i.severity])
