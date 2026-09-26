from __future__ import annotations

import pytest

from rfq_agent.agents.review_rules import RULES, feature_it_grade, review_line
from rfq_agent.data.repositories import MaterialRepo, PartRepo
from rfq_agent.models import (
    DrawingSpec,
    Envelope,
    Feature,
    FeatureType,
    PartsListItem,
    RFQItem,
    Severity,
    ShapeClass,
    TitleBlock,
    Tolerance,
)

ALL_IDS = {"VAL-001", "VAL-002", "VAL-003", "VAL-004", "VAL-005", "VAL-010", "VAL-011"} | {
    f"DFM-00{i}" for i in range(1, 8)
}


class FakeParts(PartRepo):
    """PartRepo with a fixed set of known internal part numbers (history may not be generated yet)."""

    known = {"HS-5101", "SH-5102"}

    def by_number(self, part_number: str) -> dict | None:
        return {"part_number": part_number} if part_number in self.known else None


@pytest.fixture
def repos(db):
    return MaterialRepo(db), FakeParts(db)


def item(qty=(200, 500), ref="X.pdf") -> RFQItem:
    return RFQItem(line_no=1, drawing_ref=ref, quantities=list(qty))


def spec(
    *features: Feature,
    material: str | None = "42CrMo4+QT",
    general_tolerance: str | None = "ISO 2768-mK",
    shape: ShapeClass = ShapeClass.ROTATIONAL,
    **kw,
) -> DrawingSpec:
    return DrawingSpec(
        drawing_file="X.pdf",
        title_block=TitleBlock(part_number="X-1", material=material, general_tolerance=general_tolerance),
        envelope=Envelope(shape_class=shape, max_diameter_mm=40, length_mm=220),
        features=list(features),
        **kw,
    )


def feat(id="F1", type=FeatureType.OUTER_DIAMETER, nominal=40.0, **kw) -> Feature:
    return Feature(
        id=id, type=type, description=kw.pop("description", f"{type} {nominal}"), nominal_mm=nominal, **kw
    )


def run(repos, s, it=None):
    materials, parts = repos
    return review_line(line_no=1, item=it or item(), spec=s, materials=materials, parts=parts)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def test_registry_complete():
    ids = [r.id for r in RULES]
    assert set(ids) == ALL_IDS and len(ids) == len(set(ids))
    for r in RULES:
        assert r.description and r.suggestion and "#" in r.kb_ref


def test_clean_spec_has_no_issues(repos):
    s = spec(
        feat("F1", nominal=40.0, tolerance=Tolerance(upper=0, lower=-0.062, fit="h9")),
        feat("F2", FeatureType.HOLE, nominal=8.0, length_mm=30.0),
        feat("F3", FeatureType.THREAD, nominal=12.0, length_mm=20.0, thread_spec="M12x1.75"),
    )
    issues = run(repos, s)
    assert issues == []
    assert not any(i.severity == Severity.BLOCKER for i in issues)


def test_val010_no_drawing(repos):
    issues = run(repos, None)
    assert codes(issues) == {"VAL-010"}
    assert issues[0].severity == Severity.BLOCKER
    assert issues[0].kb_ref == "quoting_policy.md#rfq-completeness"


def test_val001_material_missing(repos):
    issues = run(repos, spec(material=None))
    assert "VAL-001" in codes(issues) and "VAL-002" not in codes(issues)
    assert run(repos, spec(material="   "))[0].code == "VAL-001"


def test_val002_material_unmapped(repos):
    issues = run(repos, spec(material="Unobtainium 7"))
    assert codes(issues) == {"VAL-002"}
    assert issues[0].severity == Severity.WARNING


def test_material_code_from_postprocess_is_trusted(repos):
    s = spec(material="Sondergüte XY")
    s.title_block.material_code = "C45"
    assert run(repos, s) == []


def test_val003_quantity_missing(repos):
    issues = run(repos, spec(), item(qty=()))
    assert codes(issues) == {"VAL-003"}
    assert "VAL-003" in codes(run(repos, None, item(qty=())))


def test_val004_005_general_tolerance(repos):
    s = spec(
        feat("F1", nominal=40.0),
        feat("F2", nominal=30.0, tolerance=Tolerance(fit="h9")),
        general_tolerance=None,
    )
    issues = run(repos, s)
    assert codes(issues) == {"VAL-004", "VAL-005"}
    v5 = next(i for i in issues if i.code == "VAL-005")
    assert v5.severity == Severity.INFO and "F1" in v5.message and "F2" not in v5.message
    assert codes(run(repos, spec(feat("F1", nominal=40.0), general_tolerance=None))) == {"VAL-004", "VAL-005"}
    # general tolerance present -> untoleranced features are fine
    assert run(repos, spec(feat("F1", nominal=40.0))) == []


def test_val011_new_part_in_assembly(repos):
    parts_list = [
        PartsListItem(item_no=1, part_number="HS-5101", description="Housing", quantity=1),
        PartsListItem(item_no=2, part_number="SH-5102", description="Shaft", quantity=1),
        PartsListItem(
            item_no=3,
            part_number="6204-2RS",
            description="Deep groove ball bearing",
            quantity=2,
            standard="DIN 625",
        ),
        PartsListItem(item_no=4, description="Radial shaft seal 20x35x7", quantity=1, standard="DIN 3760"),
        PartsListItem(item_no=5, description="Socket head cap screw M5x16", quantity=4, standard="ISO 4762"),
        PartsListItem(item_no=6, part_number="EC-5103", description="End cover", quantity=1),
    ]
    s = spec(material=None, general_tolerance=None, shape=ShapeClass.ASSEMBLY, parts_list=parts_list)
    issues = run(repos, s, item(qty=(50,)))
    assert codes(issues) == {"VAL-011"}
    assert len(issues) == 1 and "EC-5103" in issues[0].message
    assert issues[0].field_path == "parts_list[5]"


def test_dfm001_thin_wall_thresholds(repos):
    wall = feat("F1", FeatureType.WALL, nominal=1.8)
    assert codes(run(repos, spec(wall, material="C45"))) == {"DFM-001"}
    assert run(repos, spec(wall, material="EN AW-6082 T6")) == []  # aluminium limit 1.5
    thin_alu = feat("F1", FeatureType.WALL, nominal=1.2)
    assert codes(run(repos, spec(thin_alu, material="EN AW-6082 T6"))) == {"DFM-001"}
    # unknown material -> steel limit; together with the blocker for the empty field
    assert codes(run(repos, spec(feat("F1", FeatureType.WALL, nominal=1.5), material=None))) == {
        "VAL-001",
        "DFM-001",
    }


def test_dfm002_deep_hole(repos):
    issues = run(repos, spec(feat("F1", FeatureType.HOLE, nominal=6.0, length_mm=80.0)))
    assert codes(issues) == {"DFM-002"}
    assert "13.3" in issues[0].message and issues[0].field_path == "features[0].length_mm"
    assert run(repos, spec(feat("F1", FeatureType.HOLE, nominal=6.0, length_mm=60.0))) == []  # L/D = 10


def test_dfm003_it_grade(repos):
    by_grade = feat("F1", nominal=35.0, tolerance=Tolerance(upper=0.018, lower=0.002, it_grade=6))
    assert codes(run(repos, spec(by_grade))) == {"DFM-003"}
    by_fit = feat("F1", nominal=35.0, tolerance=Tolerance(fit="k6"))
    assert codes(run(repos, spec(by_fit))) == {"DFM-003"}
    assert run(repos, spec(feat("F1", nominal=80.0, tolerance=Tolerance(fit="H7")))) == []


def test_feature_it_grade_fallbacks():
    assert feature_it_grade(feat(tolerance=Tolerance(fit="H7/g6"))) == 6
    assert feature_it_grade(feat(tolerance=Tolerance(fit="js5"))) == 5
    assert feature_it_grade(feat(tolerance=Tolerance(fit="k6", it_grade=7))) == 7
    assert feature_it_grade(feat(tolerance=Tolerance(upper=0.1, lower=-0.1))) is None
    assert feature_it_grade(feat()) is None


def test_dfm004_fine_surface(repos):
    assert codes(run(repos, spec(feat("F1", ra_um=0.4)))) == {"DFM-004"}
    assert run(repos, spec(feat("F1", ra_um=0.8))) == []
    s = spec()
    s.title_block.default_ra_um = 0.2
    assert codes(run(repos, s)) == {"DFM-004"}


def test_dfm005_heat_treatment_sequence(repos):
    precise = feat("F1", nominal=35.0, tolerance=Tolerance(fit="k6"), ra_um=0.8)
    issues = run(repos, spec(precise, heat_treatment="QT 28-32 HRC"))
    assert codes(issues) == {"DFM-003", "DFM-005"}
    assert codes(run(repos, spec(feat("F1", ra_um=0.8), heat_treatment="QT"))) == {"DFM-005"}
    assert (
        run(repos, spec(feat("F1", nominal=40.0, tolerance=Tolerance(fit="h9")), heat_treatment="QT")) == []
    )


@pytest.mark.parametrize(
    "desc, flagged",
    [
        ("Pocket 60x40x15, sharp internal corners", True),
        ("Pocket 60x40x15, R0", True),
        ("Pocket 60x40x15, corner radius R0.3", True),
        ("Pocket 60x40x15, R0.5", False),
        ("Pocket 60x40x15, R6", False),
        ("Pocket 60x40x15", False),  # no radius stated -> not flagged
    ],
)
def test_dfm006_internal_corners(repos, desc, flagged):
    s = spec(feat("F1", FeatureType.POCKET, nominal=40.0, description=desc), shape=ShapeClass.PRISMATIC)
    assert (codes(run(repos, s)) == {"DFM-006"}) is flagged


def test_dfm006_from_notes(repos):
    s = spec(notes=["Internal corners sharp unless otherwise specified"], shape=ShapeClass.PRISMATIC)
    issues = run(repos, s)
    assert codes(issues) == {"DFM-006"} and issues[0].field_path == "notes[0]"


def test_dfm007_thread_depth(repos):
    deep = feat("F1", FeatureType.THREAD, nominal=None, length_mm=20.0, thread_spec="M6x1")
    issues = run(repos, spec(deep))
    assert codes(issues) == {"DFM-007"} and issues[0].severity == Severity.INFO
    assert run(repos, spec(feat("F1", FeatureType.THREAD, nominal=6.0, length_mm=12.0))) == []


def test_bracket_scenario_order_and_refs(repos):
    """RFQ-0103-like bracket: empty material, 1.5 mm wall, Ø6×80 hole."""
    s = spec(
        feat("F1", FeatureType.WALL, nominal=1.5),
        feat("F2", FeatureType.HOLE, nominal=6.0, length_mm=80.0),
        material=None,
        shape=ShapeClass.PRISMATIC,
    )
    issues = run(repos, s)
    assert [i.code for i in issues] == ["VAL-001", "DFM-001", "DFM-002"]
    assert issues[0].severity == Severity.BLOCKER
    assert all(i.kb_ref and i.line_no == 1 and i.suggestion for i in issues)
