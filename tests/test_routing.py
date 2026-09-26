from __future__ import annotations

import pytest
from gen_master_data import load_master
from test_geometry import shaft_spec

from rfq_agent.agents.geometry import envelope_volume_cm3
from rfq_agent.agents.routing_rules import OP_ORDER, calibrate, plan_routing, rule_routing
from rfq_agent.data.db import init_db
from rfq_agent.data.repositories import MaterialRepo, PartRepo, ServiceRepo
from rfq_agent.models import (
    DrawingSpec,
    Envelope,
    Feature,
    FeatureType,
    PartsListItem,
    ShapeClass,
    SimilarPart,
    TitleBlock,
    Tolerance,
)


@pytest.fixture(scope="module")
def master(tmp_path_factory):
    conn = init_db(tmp_path_factory.mktemp("routing") / "r.db")
    load_master(conn)
    # fake reference shaft Ø38×210 with a routing incl. an op the rules never generate
    conn.execute(
        "INSERT INTO parts (part_id, part_number, title, family, shape_class, material_code, "
        "max_diameter_mm, length_mm) VALUES (901, 'SH-REF-01', 'Ref shaft', 'shaft', 'rotational', '42CrMo4', 38, 210)"
    )
    conn.executemany(
        "INSERT INTO routings VALUES (901, ?, ?, ?, ?, ?, ?)",
        [
            (10, "SAW", "SAW", 5, 1.5, None),
            (20, "TURN", "CNC_TURN", 40, 9.0, None),
            (30, "KEYWAY", "CNC_MILL_3AX", 20, 2.0, None),
            (40, "HEAT_TREAT", "OUTSOURCED", 0, 0, "HT_QT"),
            (50, "STRAIGHTEN", "INSPECT", 10, 3.0, None),
            (60, "GRIND", "GRIND_CYL", 25, 5.0, None),
            (70, "DEBURR", "DEBURR", 0, 1.0, None),
            (80, "INSPECT", "INSPECT_CMM", 15, 4.0, None),
            (90, "WASH_PACK", "WASH_PACK", 0, 0.3, None),
        ],
    )
    conn.commit()
    return conn


@pytest.fixture(scope="module")
def mats(master):
    return MaterialRepo(master)


@pytest.fixture(scope="module")
def services(master):
    return ServiceRepo(master).all


def work_centers(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT code FROM work_centers")}


def flange_spec() -> DrawingSpec:
    return DrawingSpec(
        drawing_file="FL-TEST.pdf",
        title_block=TitleBlock(part_number="FL-9003", material="EN AW-6082 T6"),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=120, length_mm=25),
        features=[
            Feature(
                id="F1", type=FeatureType.OUTER_DIAMETER, description="Ø120", nominal_mm=120, length_mm=15
            ),
            Feature(
                id="F2",
                type=FeatureType.OUTER_DIAMETER,
                description="Spigot Ø80 H7",
                nominal_mm=80,
                length_mm=10,
                tolerance=Tolerance(upper=0.03, lower=0.0, fit="H7"),
                ra_um=1.6,
            ),
            Feature(
                id="F3", type=FeatureType.THREAD, description="4× M6 on PCD 100", nominal_mm=6, quantity=4
            ),
            Feature(
                id="F4",
                type=FeatureType.HOLE,
                description="4× Ø9 through",
                nominal_mm=9,
                length_mm=15,
                quantity=4,
            ),
            Feature(id="F5", type=FeatureType.SPLINE, description="Involute spline", nominal_mm=20),
        ],
        surface_treatment="anodized black",
    )


def test_shaft_route_order_and_codes(master, mats, services):
    ops, uncovered = rule_routing(shaft_spec(), mats.get("42CrMo4"), services)
    codes = [o.op_code for o in ops]
    assert codes == [
        "SAW",
        "TURN",
        "DRILL",
        "KEYWAY",
        "HEAT_TREAT",
        "GRIND",
        "DEBURR",
        "INSPECT",
        "WASH_PACK",
    ]
    assert [o.seq for o in ops] == list(range(10, 10 * len(ops) + 1, 10))
    assert [OP_ORDER.index(c) for c in codes] == sorted(OP_ORDER.index(c) for c in codes)
    by = {o.op_code: o for o in ops}
    assert by["KEYWAY"].work_center == "CNC_MILL_3AX"
    assert by["GRIND"].work_center == "GRIND_CYL" and by["GRIND"].rule_ids == ["R08"]
    ht = by["HEAT_TREAT"]
    assert ht.outsourced and ht.service_code == "HT_QT" and ht.setup_min == 0 and ht.cycle_min == 0
    wcs = work_centers(master)
    assert all(o.work_center in wcs for o in ops if not o.outsourced)
    assert all(o.basis == "rule" and o.rule_cycle_min == o.cycle_min for o in ops)
    # magnitudes: turning a few minutes, not hours
    assert 2 < by["TURN"].cycle_min < 15
    assert by["TURN"].setup_min == 40  # stepped shaft → both ends
    assert by["INSPECT"].work_center == "INSPECT"  # only 2 features ≤ IT7
    assert uncovered == []


def test_flange_anodize_no_grinding(master, mats, services):
    ops, uncovered = rule_routing(flange_spec(), mats.get("AW6082"), services)
    codes = [o.op_code for o in ops]
    assert "GRIND" not in codes
    surf = next(o for o in ops if o.op_code == "SURFACE")
    assert surf.outsourced and surf.service_code == "ANODIZE" and surf.confidence >= 0.8
    drill = next(o for o in ops if o.op_code == "DRILL")
    assert drill.rule_ids == ["R04"] and drill.cycle_min > 0
    assert uncovered == ["F5"]  # spline has no rule


def test_prismatic_mill_merges_holes(master, mats, services):
    spec = DrawingSpec(
        drawing_file="BR.pdf",
        title_block=TitleBlock(),
        envelope=Envelope(shape_class=ShapeClass.PRISMATIC, length_mm=160, width_mm=80, height_mm=60),
        features=[
            Feature(id="F1", type=FeatureType.POCKET, description="Pocket", nominal_mm=40, length_mm=60),
            Feature(id="F2", type=FeatureType.HOLE, description="Ø6 deep", nominal_mm=6, length_mm=80),
            Feature(id="F3", type=FeatureType.THREAD, description="M8", nominal_mm=8, quantity=2),
            Feature(
                id="F4",
                type=FeatureType.FACE,
                description="Mounting face",
                ra_um=0.8,
            ),
        ],
        surface_treatment="zinc plated",
    )
    ops, uncovered = rule_routing(spec, None, services)
    codes = [o.op_code for o in ops]
    assert "DRILL" not in codes and "TURN" not in codes
    mill = next(o for o in ops if o.op_code == "MILL")
    assert mill.work_center == "CNC_MILL_3AX" and mill.rule_ids == ["R03", "R04"]
    assert next(o for o in ops if o.op_code == "GRIND").work_center == "GRIND_SURF"
    assert next(o for o in ops if o.op_code == "SURFACE").service_code == "ZINC"
    assert all(o.confidence < 0.7 for o in ops if not o.outsourced)  # no material → less certain
    assert uncovered == []


def test_case_hardened_gear(master, mats, services):
    spec = DrawingSpec(
        drawing_file="GR.pdf",
        title_block=TitleBlock(material="16MnCr5"),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=84, length_mm=30),
        features=[
            Feature(
                id="F1",
                type=FeatureType.GEAR_TEETH,
                description="m2 z40",
                gear_module=2,
                gear_teeth=40,
                face_width_mm=20,
            )
        ],
        heat_treatment="case hardened 58-62 HRC",
    )
    ops, _ = rule_routing(spec, mats.get("16MnCr5"), services)
    hob = next(o for o in ops if o.op_code == "HOB")
    assert hob.cycle_min == pytest.approx(0.12 * 40 * 20 / 10 * 2**0.5, abs=0.01)
    assert next(o for o in ops if o.op_code == "HEAT_TREAT").service_code == "HT_CASE"
    spec.heat_treatment = "nitrided"
    _, uncovered = rule_routing(spec, mats.get("16MnCr5"), services)
    assert "heat_treatment" in uncovered


def test_assembly_routing(services):
    spec = DrawingSpec(
        drawing_file="ASM.pdf",
        title_block=TitleBlock(),
        envelope=Envelope(shape_class=ShapeClass.ASSEMBLY),
        parts_list=[
            PartsListItem(item_no=1, part_number="HS-1", description="Housing", quantity=1),
            PartsListItem(
                item_no=2, description="Socket head cap screw M5x16", standard="ISO 4762", quantity=4
            ),
            PartsListItem(
                item_no=3, part_number="6204-2RS", description="Bearing", standard="DIN 625", quantity=2
            ),
        ],
    )
    ops, uncovered = rule_routing(spec, None, services)
    assert [(o.op_code, o.rule_ids[0]) for o in ops] == [
        ("ASSEMBLY", "R12"),
        ("TEST", "R13"),
        ("WASH_PACK", "R14"),
    ]
    assert ops[0].cycle_min == pytest.approx(1.2 * 3 + 0.3 * 4)
    assert uncovered == []


def _similar(score: float) -> list[SimilarPart]:
    return [
        SimilarPart(
            part_id=901,
            part_number="SH-REF-01",
            title="Ref shaft",
            family="shaft",
            score=score,
            score_breakdown={},
            material_code="42CrMo4",
        )
    ]


def test_calibrate_blends_and_suggests(master, mats, services):
    spec = shaft_spec()
    ops, _ = rule_routing(spec, mats.get("42CrMo4"), services)
    out = calibrate(ops, spec, _similar(0.8), PartRepo(master))
    w = 0.8**2
    scale = (envelope_volume_cm3(spec) / envelope_volume_cm3(_ref_env())) ** (2 / 3)
    turn = next(o for o in out if o.op_code == "TURN")
    rule_turn = next(o for o in ops if o.op_code == "TURN").cycle_min
    assert turn.basis == "blend" and turn.ref_part_id == 901 and turn.blend_weight == pytest.approx(w)
    assert turn.rule_cycle_min == rule_turn
    assert turn.cycle_min == pytest.approx(w * 9.0 * scale + (1 - w) * rule_turn, abs=0.01)
    assert min(rule_turn, 9.0 * scale) < turn.cycle_min < max(rule_turn, 9.0 * scale)
    # ref uses CMM, rules chose manual INSPECT → INSPECT blends with the STRAIGHTEN op (same work centre)
    sugg = [o for o in out if o.suggested]
    assert [o.work_center for o in sugg] == ["INSPECT_CMM"]
    assert sugg[0].ref_part_id == 901 and sugg[0].rule_ids == []
    ht = next(o for o in out if o.op_code == "HEAT_TREAT")
    assert ht.basis == "rule" and ht.cycle_min == 0
    assert [o.seq for o in out] == list(range(10, 10 * len(out) + 1, 10))


def _ref_env() -> DrawingSpec:
    return DrawingSpec(
        drawing_file="",
        title_block=TitleBlock(),
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=38, length_mm=210),
    )


def test_calibrate_below_threshold_is_noop(master, mats, services):
    spec = shaft_spec()
    ops, _ = rule_routing(spec, mats.get("42CrMo4"), services)
    assert calibrate(ops, spec, _similar(0.55), PartRepo(master)) == ops
    assert calibrate(ops, spec, [], PartRepo(master)) == ops


def test_plan_routing(master, mats, services):
    ops, uncovered = plan_routing(
        shaft_spec(), mats.get("42CrMo4"), services, _similar(0.9), PartRepo(master)
    )
    assert any(o.basis == "blend" for o in ops) and uncovered == []
