from __future__ import annotations

import pytest

from rfq_agent.agents.postprocess import fit_limits, it_grade, it_tolerance_um, postprocess
from rfq_agent.data.repositories import MaterialRepo
from rfq_agent.models import (
    DrawingSpec,
    Envelope,
    Evidence,
    Feature,
    FeatureType,
    PartsListItem,
    ShapeClass,
    TitleBlock,
    Tolerance,
)


@pytest.fixture(scope="module")
def materials(db):
    return MaterialRepo(db)


def test_it_grade_from_fit_code():
    assert it_grade(35, None, None, "k6") == 6
    assert it_grade(80, None, None, "H7") == 7
    assert it_grade(20, 0.021, 0.0, "H7/g6") == 7


def test_it_grade_from_limits():
    assert it_grade(35, 0.018, 0.002, None) == 6  # k6 band 16 µm
    assert it_grade(80, 0.030, 0.0, None) == 7
    # Ø40 ±0.1: band 200 µm; IT11 = 160, IT10 = 100 in the 30–50 range -> nearest in IT5–IT11 is IT11
    assert it_grade(40, 0.1, -0.1, None) == 11
    assert it_grade(40, 0.05, -0.05, None) == 10  # 100 µm = IT10 exactly
    assert it_grade(40, None, -0.1, None) is None
    assert it_grade(600, 0.1, -0.1, None) is None  # outside table


def test_it_table_lookup_and_fit_limits():
    assert it_tolerance_um(30, 7) == 21  # 30 belongs to 18–30
    assert it_tolerance_um(30.5, 7) == 25
    assert fit_limits(80, "H7") == (0.030, 0.0)
    assert fit_limits(40, "h6") == (0.0, -0.016)
    assert fit_limits(35, "k6") is None


def _spec(**kw) -> DrawingSpec:
    base = dict(
        drawing_file="X.pdf",
        title_block=TitleBlock(material="1.7225"),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=45, length_mm=225),
        features=[
            Feature(
                id="F1",
                type=FeatureType.OUTER_DIAMETER,
                description="bearing seat",
                nominal_mm=35,
                length_mm=40,
                tolerance=Tolerance(fit="k6"),
                evidence=Evidence(text="Ø35 k6"),
            ),
            Feature(
                id="F2",
                type=FeatureType.BORE,
                description="bore",
                nominal_mm=20,
                tolerance=Tolerance(fit="H7"),
                evidence=Evidence(text="Ø20 H7"),
            ),
        ],
    )
    base.update(kw)
    return DrawingSpec(**base)


def test_postprocess_material_and_it_grades(materials):
    out = postprocess(_spec(), materials)
    assert out.title_block.material_code == "42CrMo4"
    assert out.features[0].tolerance.it_grade == 6
    f2 = out.features[1].tolerance
    assert f2.it_grade == 7 and (f2.upper, f2.lower) == (0.021, 0.0)
    assert out.extraction_warnings == []


def test_postprocess_does_not_mutate_input(materials):
    spec = _spec()
    postprocess(spec, materials)
    assert spec.title_block.material_code is None


def test_material_aliases_and_missing(materials):
    out = postprocess(_spec(title_block=TitleBlock(material="42CrMo4+QT")), materials)
    assert out.title_block.material_code == "42CrMo4"
    out = postprocess(_spec(title_block=TitleBlock(material=None)), materials)
    assert out.title_block.material_code is None
    assert any("No material" in w for w in out.extraction_warnings)
    out = postprocess(_spec(title_block=TitleBlock(material="Unobtainium 7")), materials)
    assert any("not found" in w for w in out.extraction_warnings)


def test_inch_conversion(materials):
    spec = _spec(
        title_block=TitleBlock(material="C45", units="inch"),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=2, length_mm=10),
        features=[
            Feature(
                id="F1",
                type=FeatureType.OUTER_DIAMETER,
                description="od",
                nominal_mm=1.5,
                tolerance=Tolerance(upper=0.001, lower=-0.001),
                evidence=Evidence(text="Ø1.500 ±.001"),
            )
        ],
    )
    out = postprocess(spec, materials)
    assert out.title_block.units == "mm"
    assert out.envelope.max_diameter_mm == pytest.approx(50.8)
    assert out.features[0].nominal_mm == pytest.approx(38.1)
    assert out.features[0].tolerance.upper == pytest.approx(0.0254)
    assert out.features[0].tolerance.it_grade == 9  # band 50.8 µm at Ø38.1: IT8 = 39, IT9 = 62 -> IT9 nearer
    # idempotent: second pass does not convert again
    assert postprocess(out, materials).envelope.max_diameter_mm == pytest.approx(50.8)


def test_envelope_derivation_and_consistency(materials):
    feats = [
        Feature(id="F1", type=FeatureType.OUTER_DIAMETER, description="a", nominal_mm=30, length_mm=50),
        Feature(id="F2", type=FeatureType.OUTER_DIAMETER, description="b", nominal_mm=40, length_mm=100),
        Feature(id="F3", type=FeatureType.THREAD, description="thread", nominal_mm=12),
    ]
    spec = _spec(envelope=Envelope(shape_class=ShapeClass.ROTATIONAL), features=feats)
    out = postprocess(spec, materials)
    assert out.envelope.max_diameter_mm == 40 and out.envelope.length_mm == 150
    w = " | ".join(out.extraction_warnings)
    assert "derived" in w and "F3: thread without" in w and "F1: no evidence" in w

    spec = _spec(
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=35, length_mm=100),
        features=feats,
    )
    w = " | ".join(postprocess(spec, materials).extraction_warnings)
    assert "F2: Ø40" in w and "F2: length 100" not in w


def test_parts_list_forces_assembly(materials):
    spec = _spec(
        title_block=TitleBlock(),
        features=[],
        parts_list=[
            PartsListItem(item_no=1, description="Shaft", quantity=1),
            PartsListItem(item_no=2, description="Bearing 6204-2RS", quantity=2),
        ],
    )
    out = postprocess(spec, materials)
    assert out.envelope.shape_class == ShapeClass.ASSEMBLY
    assert any("set to assembly" in w for w in out.extraction_warnings)
    assert not any("No material" in w for w in out.extraction_warnings)
