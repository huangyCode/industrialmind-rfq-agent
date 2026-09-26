from __future__ import annotations

import math

import pytest

from rfq_agent.agents.geometry import (
    envelope_volume_cm3,
    feature_it_grade,
    finished_volume_cm3,
    select_stock,
    weight_kg,
)
from rfq_agent.models import DrawingSpec, Envelope, Feature, FeatureType, ShapeClass, TitleBlock, Tolerance


def shaft_spec() -> DrawingSpec:
    """Output shaft Ø40×220, 42CrMo4 QT, two Ø35 k6 Ra0.8 bearing seats, keyway, M12 centre hole."""
    k6 = Tolerance(upper=0.018, lower=0.002, fit="k6")
    return DrawingSpec(
        drawing_file="SH-TEST.pdf",
        title_block=TitleBlock(part_number="SH-9001", title="Output shaft", material="42CrMo4+QT"),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=40, length_mm=220),
        features=[
            Feature(
                id="F1",
                type=FeatureType.OUTER_DIAMETER,
                description="Ø40 collar",
                nominal_mm=40,
                length_mm=60,
            ),
            Feature(
                id="F2",
                type=FeatureType.OUTER_DIAMETER,
                description="Ø35 k6 bearing seat",
                nominal_mm=35,
                length_mm=25,
                tolerance=k6,
                ra_um=0.8,
            ),
            Feature(
                id="F3",
                type=FeatureType.OUTER_DIAMETER,
                description="Ø35 k6 bearing seat",
                nominal_mm=35,
                length_mm=25,
                tolerance=k6,
                ra_um=0.8,
            ),
            Feature(
                id="F4",
                type=FeatureType.OUTER_DIAMETER,
                description="Ø32 drive end",
                nominal_mm=32,
                length_mm=80,
            ),
            Feature(
                id="F5", type=FeatureType.KEYWAY, description="Keyway 10P9 × 40", nominal_mm=10, length_mm=40
            ),
            Feature(
                id="F6",
                type=FeatureType.THREAD,
                description="Centre hole M12 DIN 332-D",
                nominal_mm=12,
                length_mm=24,
                thread_spec="M12x1.75",
            ),
            Feature(id="F7", type=FeatureType.CHAMFER, description="Chamfer 1×45°", quantity=4),
            Feature(id="F8", type=FeatureType.GROOVE, description="Undercut DIN 509", quantity=2),
        ],
        heat_treatment="QT 28-32 HRC",
    )


def bracket_spec(**env) -> DrawingSpec:
    return DrawingSpec(
        drawing_file="BR-TEST.pdf",
        title_block=TitleBlock(part_number="BR-9002"),
        envelope=Envelope(shape_class=ShapeClass.PRISMATIC, length_mm=160, width_mm=80, height_mm=60, **env),
        features=[
            Feature(
                id="F1",
                type=FeatureType.HOLE,
                description="Ø9 through",
                nominal_mm=9,
                length_mm=60,
                quantity=4,
            ),
        ],
    )


def test_envelope_rotational():
    v = envelope_volume_cm3(shaft_spec())
    assert v == pytest.approx(math.pi / 4 * 4.0**2 * 22.0, rel=1e-6)


def test_finished_volume_rotational_segments():
    spec = shaft_spec()
    fin = finished_volume_cm3(spec)
    env = envelope_volume_cm3(spec)
    # Σ segments (190 mm listed + 30 mm remainder at Ø32) is well below the envelope
    assert 0.6 * env < fin < env
    spec.features = [f for f in spec.features if f.type != FeatureType.OUTER_DIAMETER]
    assert finished_volume_cm3(spec) == pytest.approx(env * 0.7)


def test_prismatic_volumes_and_plate():
    spec = bracket_spec()
    env = envelope_volume_cm3(spec)
    assert env == pytest.approx(16 * 8 * 6)
    assert finished_volume_cm3(spec) < env
    spec.features = []
    assert finished_volume_cm3(spec) == pytest.approx(env * 0.6)
    st = select_stock(spec)
    assert st.kind == "plate" and st.thickness_mm == 70 and st.width_mm == 83 and st.length_mm == 163


def test_bar_stock_selection():
    st = select_stock(shaft_spec(), "42CrMo4")
    assert st.kind == "bar" and st.diameter_mm == 45 and st.length_mm == 225
    assert "Ø45" in st.description and "42CrMo4" in st.description
    assert weight_kg(st.volume_cm3, 7.85) == pytest.approx(math.pi / 4 * 4.5**2 * 22.5 * 7.85 / 1000)


def test_bar_beyond_series_is_flagged():
    spec = shaft_spec()
    spec.envelope.max_diameter_mm = 260
    st = select_stock(spec)
    assert st.diameter_mm == 270 and "non-standard" in st.description


def test_it_grade_fallbacks():
    assert feature_it_grade(35, Tolerance(fit="k6")) == 6
    assert feature_it_grade(80, Tolerance(fit="H7")) == 7
    assert feature_it_grade(35, Tolerance(upper=0.018, lower=0.002)) == 6  # 16 µm band at Ø35
    assert feature_it_grade(35, Tolerance(upper=0.1, lower=-0.1)) >= 11
    assert feature_it_grade(35, None) is None
