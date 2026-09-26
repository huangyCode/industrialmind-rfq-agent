"""Synthetic history (scripts/gen_history.py) and the similarity → routing → cost → confidence chain."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date

import pytest
from gen_history import (
    FAMILY_BIAS,
    PLANTS,
    QTYS,
    build_history,
    hidden_routing,
    learning,
    load_specs,
)
from gen_master_data import load_master

from rfq_agent.agents.bom import build_bom
from rfq_agent.agents.confidence import assess
from rfq_agent.agents.costing import compute_costs, price_deviation, recommend_plant
from rfq_agent.agents.geometry import envelope_volume_cm3, finished_volume_cm3, weight_kg
from rfq_agent.agents.postprocess import postprocess
from rfq_agent.agents.review_rules import review_line
from rfq_agent.agents.routing_rules import OP_ORDER, OUTSOURCED_WC, plan_routing, rule_routing
from rfq_agent.agents.similarity import find_similar, reuse_hint
from rfq_agent.config import SAMPLES_DIR
from rfq_agent.data.db import init_db
from rfq_agent.data.repositories import CatalogRepo, MaterialRepo, PartRepo, RateRepo, ServiceRepo
from rfq_agent.models import DrawingSpec, RFQRequest, ShapeClass, Tier

EXTRA_OPS = {"STRAIGHTEN", "MILL_FINISH", "TOOTH_CHAMFER"}
TODAY = date(2026, 9, 26)


@pytest.fixture(scope="module")
def repos(db):
    return MaterialRepo(db), PartRepo(db), RateRepo(db), ServiceRepo(db).all, CatalogRepo(db)


# ---------- history content ----------


def test_family_counts(db):
    fams = Counter(r["family"] for r in PartRepo(db).all())
    assert fams == {"shaft": 25, "flange": 20, "bracket": 15, "housing": 10, "gear": 12, "assembly": 8}


def test_specs_valid_and_consistent(db):
    parts = PartRepo(db)
    recs = load_specs()
    assert len(recs) == 90
    for rec in recs:
        spec: DrawingSpec = rec["spec"]
        row = parts.get(rec["part_id"])
        assert spec.title_block.part_number == row["part_number"]
        assert spec.extraction_warnings == [], rec["part_number"]
        assert DrawingSpec.model_validate_json(row["spec_json"]) == spec
        if spec.envelope.shape_class == ShapeClass.ASSEMBLY:
            assert spec.parts_list and row["material_code"] is None
        else:
            assert spec.title_block.material_code == row["material_code"] is not None
            assert spec.features and all(f.evidence for f in spec.features)


def test_routings_follow_conventions(db, repos):
    _, parts, rates, services, _ = repos
    known_wc = {wc for _, wc in rates.rates} | {OUTSOURCED_WC}
    seen = set()
    for row in parts.all():
        ops = parts.routing(row["part_id"])
        assert ops
        for op in ops:
            seen.add(op["op_code"])
            assert op["op_code"] in OP_ORDER or op["op_code"] in EXTRA_OPS
            assert op["work_center"] in known_wc
            if op["outsourced"]:
                assert op["work_center"] == OUTSOURCED_WC and op["service_code"] in services
                assert op["setup_min"] == op["cycle_min"] == 0
            else:
                assert op["service_code"] is None and op["cycle_min"] > 0
    assert EXTRA_OPS <= seen
    assert {"HEAT_TREAT", "SURFACE", "GRIND", "HOB", "ASSEMBLY"} <= seen


def test_part_costs_and_quotes(db, repos):
    _, parts, *_ = repos
    for row in parts.all():
        costs = {(c["plant"], c["qty"]): c["unit_cost_eur"] for c in parts.costs(row["part_id"])}
        assert {("DE", q) for q in QTYS} <= set(costs)  # DE has every work centre
        assert set(costs) <= {(p, q) for p in PLANTS for q in QTYS}
        assert costs[("DE", 50)] > costs[("DE", 1000)] > 0  # setup + small-batch penalty
        quotes = parts.quotes(row["part_id"])
        assert 1 <= len(quotes) <= 3
        for qt in quotes:
            margin = 1 - costs[(qt["plant"], qt["qty"])] / qt["unit_price_eur"]
            assert 0.11 < margin < 0.26
    grind = [
        r for r in parts.all() if any(o["work_center"] == "GRIND_CYL" for o in parts.routing(r["part_id"]))
    ]
    assert grind and all(not any(c["plant"] == "CN" for c in parts.costs(r["part_id"])) for r in grind)


def test_assembly_bom_rows(db):
    rows = db.execute("SELECT parent_part_id, child_part_number FROM assembly_bom").fetchall()
    assert len({r["parent_part_id"] for r in rows}) == 8
    parts = PartRepo(db)
    asm = parts.by_number("ASM-5050")
    children = {r["child_part_number"] for r in rows if r["parent_part_id"] == asm["part_id"]}
    assert {"HS-5101", "SH-5102", "6204-2RS"} <= children


def test_demo_anchor_parts(db):
    parts = PartRepo(db)
    for pn in ("HS-5101", "SH-5102", "SH-4650", "FL-2150", "BR-0870"):
        row = parts.by_number(pn)
        assert row is not None and parts.closest_cost(row["part_id"], 50) is not None, pn
    assert parts.by_number("EC-5103") is None
    assert parts.by_number("HS-5101")["material_code"] == "GJS500"
    assert parts.by_number("SH-5102")["material_code"] == "C45"


def test_deterministic(tmp_path):
    def snapshot(path):
        conn = init_db(path)
        load_master(conn)
        build_history(conn, write_specs=False)
        return [
            [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()]
            for t in ("parts", "routings", "part_costs", "quote_history")
        ]

    assert snapshot(tmp_path / "a.db") == snapshot(tmp_path / "b.db")


# ---------- hidden process model ----------


def test_learning_curve():
    assert learning(50) == pytest.approx(1.35)
    assert learning(50) > learning(200) > learning(1000) > 1.0


def test_hidden_model_differs_from_rules(db, repos):
    """Truth is not 'rules × constant': per-family rule bias differs in sign, extra ops exist."""
    mats, parts, rates, services, catalog = repos
    bias: dict[str, list[float]] = {}
    for rec in load_specs():
        spec = rec["spec"]
        fam = parts.get(rec["part_id"])["family"]
        mat = mats.all.get(spec.title_block.material_code or "")
        ops, _ = rule_routing(spec, mat, services)
        fin = weight_kg(finished_volume_cm3(spec), mat.density_g_cm3) if mat else 0.0
        bom = build_bom(spec, mat, ops, catalog, parts)
        (cb,) = compute_costs(
            routing=ops,
            bom=bom,
            quantities=[200],
            finished_weight_kg=fin,
            requested_delivery=None,
            today=TODAY,
            rates=rates,
            services=services,
        )[:1]
        actual = parts.closest_cost(rec["part_id"], 200, "DE")
        assert actual["plant"] == "DE" and actual["qty"] == 200
        bias.setdefault(fam, []).append(cb.unit_cost_eur / actual["unit_cost_eur"] - 1)
    med = {f: statistics.median(v) for f, v in bias.items()}
    assert min(med.values()) < -0.12 and max(med.values()) > 0.12
    assert all(-0.35 < m < 0.35 for m in med.values())
    assert set(FAMILY_BIAS) == set(med)


def test_hidden_model_is_independent_of_rule_engine():
    import inspect

    import gen_history

    src = inspect.getsource(gen_history)
    assert "from rfq_agent.agents.routing_rules" not in src and "rule_routing(" not in src
    assert hidden_routing.__doc__ and "lognormal" in hidden_routing.__doc__


def test_calibration_improves_leave_one_out(db, repos):
    """Leave-one-out at DE / 200 pcs: rules + similar-part calibration beat rules alone."""
    mats, parts, rates, services, catalog = repos
    err_rule, err_cal = [], []
    for rec in load_specs():
        spec = rec["spec"]
        mat = mats.all.get(spec.title_block.material_code or "")
        fin = weight_kg(finished_volume_cm3(spec), mat.density_g_cm3) if mat else 0.0
        sims = find_similar(spec, parts, mats, exclude_part_id=rec["part_id"])
        actual = parts.closest_cost(rec["part_id"], 200, "DE")["unit_cost_eur"]
        for ops, errs in (
            (rule_routing(spec, mat, services)[0], err_rule),
            (plan_routing(spec, mat, services, sims, parts)[0], err_cal),
        ):
            bom = build_bom(spec, mat, ops, catalog, parts)
            (cb,) = compute_costs(
                routing=ops,
                bom=bom,
                quantities=[200],
                finished_weight_kg=fin,
                requested_delivery=None,
                today=TODAY,
                rates=rates,
                services=services,
            )[:1]
            errs.append(abs(cb.unit_cost_eur / actual - 1))
    md_rule, md_cal = statistics.median(err_rule), statistics.median(err_cal)
    hit_rule = sum(e <= 0.2 for e in err_rule) / len(err_rule)
    hit_cal = sum(e <= 0.2 for e in err_cal) / len(err_cal)
    assert md_cal < md_rule - 0.02
    assert hit_cal > hit_rule + 0.1


# ---------- demo RFQs end to end (gold specs, no LLM) ----------


def run_sample(rfq: str, pn: str, repos):
    mats, parts, rates, services, catalog = repos
    d = SAMPLES_DIR / rfq / "expected"
    spec = postprocess(DrawingSpec.model_validate_json((d / f"{pn}.json").read_text(encoding="utf-8")), mats)
    item = RFQRequest.model_validate_json((d / "rfq.json").read_text(encoding="utf-8")).items[0]
    qty = item.quantities[0]
    mat = mats.all.get(spec.title_block.material_code or "")
    similar = find_similar(spec, parts, mats, qty=qty)
    routing, uncovered = plan_routing(spec, mat, services, similar, parts)
    issues = review_line(line_no=1, item=item, spec=spec, materials=mats, parts=parts)
    if hint := reuse_hint(spec, similar, 1):
        issues.append(hint)
    bom = build_bom(spec, mat, routing, catalog, parts)
    fin = weight_kg(finished_volume_cm3(spec), mat.density_g_cm3) if mat else 0.0
    costs = compute_costs(
        routing=routing,
        bom=bom,
        quantities=item.quantities,
        finished_weight_kg=fin,
        requested_delivery=item.requested_delivery,
        today=TODAY,
        rates=rates,
        services=services,
    )
    plant = recommend_plant(costs, qty)
    dev = price_deviation(costs, plant, qty, similar, envelope_volume_cm3(spec), parts)
    conf = assess(
        spec=spec,
        item=item,
        issues=issues,
        similar=similar,
        routing=routing,
        uncovered=uncovered,
        bom=bom,
        price_deviation_pct=dev,
    )
    return dict(spec=spec, similar=similar, routing=routing, issues=issues, bom=bom, dev=dev, conf=conf)


def test_rfq_0102_flange_reuse_fast_track(repos):
    r = run_sample("RFQ-2026-0102", "FL-2208", repos)
    top = r["similar"][0]
    assert top.part_number == "FL-2150" and top.score >= 0.92
    assert any(i.code == "REUSE-001" for i in r["issues"])
    assert r["conf"].tier == Tier.FAST_TRACK, r["conf"].reasons


def test_rfq_0101_shaft_blend_standard(repos):
    r = run_sample("RFQ-2026-0101", "SH-4711", repos)
    top = r["similar"][0]
    assert top.part_number == "SH-4650" and 0.6 <= top.score < 0.92
    assert not any(i.code == "REUSE-001" for i in r["issues"])
    blended = [op for op in r["routing"] if op.basis == "blend" and not op.suggested]
    assert blended and all(op.ref_part_id == top.part_id for op in blended)
    assert any(op.suggested and op.op_code == "STRAIGHTEN" for op in r["routing"])
    # the closest shaft really cost more than our estimate → price flagged → engineer review
    assert r["dev"] is not None and r["dev"] < -0.30
    assert r["conf"].tier == Tier.STANDARD, r["conf"].reasons


def test_rfq_0103_bracket_manual(repos):
    r = run_sample("RFQ-2026-0103", "BR-0930", repos)
    assert "BR-0870" in {s.part_number for s in r["similar"]}
    assert r["conf"].tier == Tier.MANUAL


def test_rfq_0104_assembly_internal_parts_standard(repos):
    r = run_sample("RFQ-2026-0104", "ASM-5100", repos)
    by_pn = {b.part_number: b for b in r["bom"]}
    for pn in ("HS-5101", "SH-5102"):
        assert by_pn[pn].source == "internal" and by_pn[pn].unit_cost_eur and by_pn[pn].unit_cost_eur > 0
    assert by_pn["EC-5103"].source == "new" and by_pn["EC-5103"].unit_cost_eur is None
    assert r["similar"][0].part_number == "ASM-5050"
    assert "Material" not in " ".join(r["spec"].extraction_warnings)
    assert r["conf"].tier == Tier.STANDARD


# ---------- postprocess fix ----------


def test_postprocess_no_material_warning_for_assembly(db):
    mats = MaterialRepo(db)
    p = SAMPLES_DIR / "RFQ-2026-0104" / "expected" / "ASM-5100.json"
    spec = postprocess(DrawingSpec.model_validate_json(p.read_text(encoding="utf-8")), mats)
    assert spec.title_block.material == "see parts list" and spec.title_block.material_code is None
    assert not any("not found in material master data" in w for w in spec.extraction_warnings)
    # a single part with an unknown material still gets the warning
    single = DrawingSpec.model_validate_json(
        (SAMPLES_DIR / "RFQ-2026-0102" / "expected" / "FL-2208.json").read_text(encoding="utf-8")
    )
    single.title_block.material = "Unobtainium 7"
    out = postprocess(single, mats)
    assert any("not found in material master data" in w for w in out.extraction_warnings)
