from __future__ import annotations

import math

import pytest
from gen_master_data import load_master
from test_geometry import shaft_spec

from rfq_agent.agents.bom import build_bom
from rfq_agent.agents.routing_rules import rule_routing
from rfq_agent.data.db import init_db
from rfq_agent.data.repositories import CatalogRepo, MaterialRepo, PartRepo, ServiceRepo
from rfq_agent.models import DrawingSpec, Envelope, MakeOrBuy, PartsListItem, ShapeClass, TitleBlock


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    c = init_db(tmp_path_factory.mktemp("bom") / "b.db")
    load_master(c)
    c.execute(
        "INSERT INTO parts (part_id, part_number, title, family, shape_class) "
        "VALUES (801, 'HS-9101', 'Actuator housing', 'housing', 'prismatic'), "
        "(802, 'SH-9102', 'Actuator shaft', 'shaft', 'rotational')"
    )
    c.executemany(
        "INSERT INTO part_costs VALUES (801, ?, ?, ?)",
        [("DE", 50, 48.0), ("DE", 200, 39.5), ("PL", 50, 36.0)],
    )
    c.commit()
    return c


def assembly_spec() -> DrawingSpec:
    return DrawingSpec(
        drawing_file="ASM-TEST.pdf",
        title_block=TitleBlock(part_number="ASM-9100", title="Actuator sub-assembly"),
        envelope=Envelope(shape_class=ShapeClass.ASSEMBLY, max_diameter_mm=90, length_mm=140),
        parts_list=[
            PartsListItem(item_no=1, part_number="HS-9101", description="Housing", quantity=1),
            PartsListItem(item_no=2, part_number="SH-9102", description="Shaft", quantity=1),
            PartsListItem(
                item_no=3,
                part_number="6204-2RS",
                description="Rillenkugellager",
                quantity=2,
                standard="DIN 625",
            ),
            PartsListItem(item_no=4, description="Wellendichtring 20x35x7", quantity=1, standard="DIN 3760"),
            PartsListItem(item_no=5, description="Zylinderschraube M5 x 16", quantity=4, standard="ISO 4762"),
            PartsListItem(item_no=6, part_number="EC-9103", description="End cover", quantity=1),
        ],
    )


def test_single_part_bom(conn):
    mats = MaterialRepo(conn)
    mat = mats.get("42CrMo4")
    spec = shaft_spec()
    routing, _ = rule_routing(spec, mat, ServiceRepo(conn).all)
    bom = build_bom(spec, mat, routing, CatalogRepo(conn), PartRepo(conn))
    raw, svc = bom
    assert raw.source == "raw_material" and raw.unit == "kg" and raw.make_or_buy == MakeOrBuy.BUY
    assert raw.qty_per == pytest.approx(math.pi / 4 * 4.5**2 * 22.5 * 7.85 / 1000, abs=1e-3)
    assert raw.unit_cost_eur == 2.40 and "Ø45 × 225" in raw.description
    assert svc.source == "service" and svc.part_number == "HT_QT" and svc.unit_cost_eur is None
    assert svc.make_or_buy == MakeOrBuy.SERVICE and 0 < svc.qty_per < raw.qty_per


def test_single_part_unknown_material(conn):
    spec = shaft_spec()
    spec.heat_treatment = None
    bom = build_bom(spec, None, [], CatalogRepo(conn), PartRepo(conn))
    assert len(bom) == 1 and bom[0].unit_cost_eur is None and bom[0].confidence < 0.5


def test_assembly_bom(conn):
    bom = build_bom(assembly_spec(), None, [], CatalogRepo(conn), PartRepo(conn))
    by = {b.item_no: b for b in bom}
    assert by[1].source == "internal" and by[1].make_or_buy == MakeOrBuy.MAKE
    assert by[1].unit_cost_eur == 39.5  # DE @ 200 is the closest batch to the default ref qty
    assert by[2].source == "internal" and by[2].unit_cost_eur is None
    assert by[3].source == "catalog" and by[3].match_method == "exact" and by[3].qty_per == 2
    assert by[4].matched_ref == "SEAL-20x35x7" and by[4].match_method == "normalized"
    assert by[5].matched_ref == "ISO4762-M5x16" and by[5].unit_cost_eur == 0.08
    new = by[6]
    assert new.source == "new" and new.unit_cost_eur is None and new.confidence <= 0.3 and new.note
    bom50 = build_bom(assembly_spec(), None, [], CatalogRepo(conn), PartRepo(conn), ref_qty=50)
    assert bom50[0].unit_cost_eur == 48.0  # priced at the assembly plant (DE), not the cheapest plant
