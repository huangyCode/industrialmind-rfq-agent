"""Eval scripts (eval/): backtest on a subset, routing check with the reference extractor, feedback report."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import backtest_costing as bt  # noqa: E402
import eval_routing as er  # noqa: E402
import feedback_report as fr  # noqa: E402

from rfq_agent.models import Tier  # noqa: E402

SUBSET = ["FL-2150", "SH-4650", "HS-5101", "SH-5102", "GR-3340", "ASM-5050"]


@pytest.fixture(scope="module")
def backtest(db):
    have = {r["part_number"] for r in db.execute("SELECT part_number FROM parts")}
    subset = [p for p in SUBSET if p in have] or None
    return bt.run_backtest(db, part_numbers=subset, limit=None if subset else 8)


def test_backtest_subset_sane(backtest):
    pts, parts = backtest["points"], backtest["parts"]
    assert parts and pts
    assert {p.method for p in pts} == set(bt.METHODS)
    # every (part, plant, qty) actual is scored once per method
    per_method = {m: sum(p.method == m for p in pts) for m in bt.METHODS}
    assert len(set(per_method.values())) == 1
    s = bt.summarize(backtest)
    for m in bt.METHODS:
        x = s["overall"][m]
        assert x["n"] > 0
        assert 0 <= x["mdape"] < 1.0 and 0 <= x["w10"] <= x["w20"] <= 1
        assert -1 < x["bias"] < 1
    # leave-one-out: a part never finds itself
    assert all(p["top_similar"] != p["part_number"] for p in parts)
    # the report renders and the plot is written
    assert "How the data was generated" in bt.render(backtest, s, "x.png")


def test_backtest_actuals_match_part_costs(db, backtest):
    p = next(x for x in backtest["points"] if x.method == "rules")
    row = db.execute(
        "SELECT unit_cost_eur FROM part_costs JOIN parts USING (part_id) WHERE part_number=? AND plant=? AND qty=?",
        (p.part_number, p.plant, p.qty),
    ).fetchone()
    assert row[0] == pytest.approx(p.actual)


def test_backtest_plot(backtest, tmp_path):
    out = tmp_path / "b.png"
    bt.plot(backtest, out)
    assert out.stat().st_size > 5_000


def _service(db, tmp_path, monkeypatch):
    """eval_routing.isolated_service, but on a copy of the session test DB instead of data/rfq.db."""
    from rfq_agent.app_api import RFQService
    from rfq_agent.data.db import connect
    from rfq_agent.graph.deps import Deps

    def isolated(workdir: Path, extractors: dict, client=None) -> RFQService:
        workdir.mkdir(parents=True, exist_ok=True)
        conn = connect(workdir / "rfq.db")
        db.backup(conn)
        deps = Deps.from_conn(
            conn, client=client, extractors=extractors, quote_dir=workdir / "q", today=er.TODAY
        )
        return RFQService(deps, workdir / "cp.db")

    monkeypatch.setattr(er, "isolated_service", isolated)
    monkeypatch.setattr(fr, "isolated_service", isolated)
    return isolated


def test_routing_reference_all_pass(db, tmp_path, monkeypatch):
    _service(db, tmp_path, monkeypatch)
    res = er.evaluate(tmp_path, llm=False)
    rows = res["reference"]
    assert [r.rfq_id for r in rows] == list(er.EXPECTED)
    assert all(r.passed for r in rows), [(r.rfq_id, r.actual, r.note) for r in rows]
    by_id = {r.rfq_id: r for r in rows}
    assert by_id["RFQ-2026-0103"].unit_price == "no price"
    assert by_id["RFQ-2026-0104"].plant == "DE"  # assembly built where its make parts are costed
    assert by_id["RFQ-2026-0102"].actual == Tier.FAST_TRACK.value
    md = er.render(res)
    assert "4/4 pass" in md and "skipped" in md


def test_feedback_demo_per_work_center(db, tmp_path, monkeypatch):
    _service(db, tmp_path, monkeypatch)
    records = fr.demo_feedback(tmp_path / "fb")
    assert {r.rfq_id for r in records} == {x[0] for x in fr.DEMO_ROUNDS}
    rows = fr.time_edits(records)
    wc = fr.by_work_center(rows)
    assert wc["CNC_TURN"]["setup"] == [pytest.approx(0.5, abs=0.01)]
    assert wc["ASSEMBLY"]["cycle"] == [pytest.approx(1.25, abs=0.01)]
    assert wc["INSPECT"]["ops"] == 1  # accepted STRAIGHTEN suggestion
    md = fr.render([], records)
    assert "Demo data" in md and "shadow mode" in md.lower()
