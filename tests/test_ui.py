"""Streamlit workbench tests (AppTest): isolated service (temp checkpoint DB / quote dir), reference extractor."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from rfq_agent import app_api
from rfq_agent.app_api import RFQService
from rfq_agent.config import SAMPLES_DIR
from rfq_agent.graph import Deps, ReferenceExtractor
from rfq_agent.models import RFQStatus, Tier
from rfq_agent.ui import common
from rfq_agent.ui.logic import business_view, price_changes, routing_edits, routing_frame

APP = Path(__file__).resolve().parents[1] / "src" / "rfq_agent" / "ui" / "app.py"
TIMEOUT = 60


@pytest.fixture
def svc(db, tmp_path):
    deps = Deps.from_conn(
        db,
        client=None,  # template texts only; no model is ever called from these tests
        extractors={"reference": ReferenceExtractor()},
        quote_dir=tmp_path / "quotes",
        today=date(2026, 9, 26),
    )
    s = RFQService(deps, tmp_path / "cp.db")
    app_api.set_service(s)
    common.reset_service_cache()
    yield s
    app_api.set_service(None)
    common.reset_service_cache()


def sample(n: str) -> Path:
    return SAMPLES_DIR / f"RFQ-2026-{n}"


def page(name: str) -> AppTest:
    """AppTest for a single workbench page (AppTest can only switch between file-based pages)."""

    def script(name):
        import importlib

        from rfq_agent.ui import common

        common.inject_css()
        importlib.import_module(f"rfq_agent.ui.views.{name}").render()

    return AppTest.from_function(script, kwargs={"name": name}, default_timeout=TIMEOUT)


def _texts(at: AppTest) -> str:
    parts = [e.value for e in (*at.markdown, *at.caption, *at.title, *at.subheader, *at.text)]
    parts += [e.value for e in (*at.success, *at.info, *at.warning, *at.error)]
    return "\n".join(str(p) for p in parts)


# ---------- app shell ----------


def test_app_loads_with_navigation(svc):
    at = AppTest.from_file(str(APP), default_timeout=TIMEOUT).run()
    assert not at.exception
    assert at.title[0].value == "Inbox"
    assert "AI RFQ & Quotation Agent" in at.sidebar.markdown[0].value
    assert len(at.button) >= 4  # one Run button per sample


@pytest.mark.parametrize("name", ["inbox", "review", "quote", "knowledge", "metrics"])
def test_each_page_renders_without_runs(svc, name):
    at = page(name).run()
    assert not at.exception, at.exception


# ---------- inbox ----------


def test_inbox_run_0102_reference_reaches_fast_track_approved(svc):
    at = page("inbox").run()
    at.radio(key="extractor").set_value("reference")
    at.checkbox(key="auto_approve").check()
    at.run()
    at.button(key="run_RFQ-2026-0102").click().run()
    assert not at.exception, at.exception
    v = svc.get("RFQ-2026-0102")
    assert v.status == RFQStatus.APPROVED.value and v.tiers == {1: Tier.FAST_TRACK}
    assert at.session_state["rfq_id"] == "RFQ-2026-0102"
    text = _texts(at)
    assert "Last run: RFQ-2026-0102" in text and "auto-approved" in text
    assert "extract_drawings" in at.dataframe[0].value["node"].tolist()


# ---------- review ----------


def test_review_0101_renders_all_tabs(svc):
    svc.start(sample("0101"), "reference")
    at = page("review")
    at.session_state["rfq_id"] = "RFQ-2026-0101"
    at.run()
    assert not at.exception, at.exception
    text = _texts(at)
    assert "Standard review" in "".join(str(b.proto) for b in at.get("badge")) or "standard" in text.lower()
    assert "Plant CN excluded" in text and "GRIND_CYL" in text  # infeasible reason shown
    assert "Why this tier" in text
    assert [t.label for t in at.tabs][:6] == [
        "Extraction",
        "Issues",
        "Similar parts",
        "BOM",
        "Routing",
        "Cost",
    ]
    kb = [e for e in at.expander if e.label.startswith("Knowledge base:")]
    assert kb and any("tolerance_guide.md#it-grades" in e.label for e in kb)
    assert any(b.label.startswith("Apply edits") for b in at.button)


def test_review_0101_margin_edit_recalculates_and_shows_change(svc):
    v0 = svc.start(sample("0101"), "reference")
    plant = v0.draft.lines[0].recommended_plant
    p0 = next(c for c in v0.draft.lines[0].costs if c.plant == plant and c.qty == 200).unit_price_eur
    at = page("review")
    at.session_state["rfq_id"] = "RFQ-2026-0101"
    at.run()
    at.number_input(key="margin_RFQ-2026-0101_0").set_value(25.0).run()
    at.text_input(key="reviewer").set_value("M. Weber").run()
    at.button(key="btn_edit").click().run()
    assert not at.exception, at.exception
    v1 = svc.get("RFQ-2026-0101")
    assert v1.review_round == 1 and v1.draft.version == 2 and v1.pending_review
    assert v1.draft.margin_pct == pytest.approx(0.25)
    p1 = next(c for c in v1.draft.lines[0].costs if c.plant == plant and c.qty == 200).unit_price_eur
    assert p1 > p0
    text = _texts(at)
    assert "What changed in the last edit" in text and "Edits applied" in text
    changes = [d.value for d in at.dataframe if "plant @ qty" in d.value.columns]
    assert changes and f"{plant}@200" in changes[0]["plant @ qty"].tolist()
    assert svc.feedback("RFQ-2026-0101")[-1].reviewer == "M. Weber"


def test_review_invalid_plant_edit_is_reported(svc):
    svc.start(sample("0101"), "reference")
    with pytest.raises(app_api.EditError):
        svc.review(
            "RFQ-2026-0101",
            {
                "action": "edit",
                "reviewer": "x",
                "edits": [{"line_no": 1, "target": "plant", "field": "plant", "new_value": "CN"}],
            },
        )
    at = page("review")
    at.session_state["rfq_id"] = "RFQ-2026-0101"
    at.session_state["review_msg"] = ("error", "Line 1: plant CN is not feasible for this routing")
    at.run()
    assert not at.exception and "not feasible" in at.error[0].value


def test_review_approve_then_quote_page(svc):
    svc.start(sample("0104"), "reference")
    at = page("review")
    at.session_state["rfq_id"] = "RFQ-2026-0104"
    at.run()
    at.button(key="btn_approve").click().run()
    assert not at.exception, at.exception
    assert svc.get("RFQ-2026-0104").status == RFQStatus.APPROVED.value
    q = page("quote")
    q.session_state["rfq_id"] = "RFQ-2026-0104"
    q.run()
    assert not q.exception, q.exception
    assert any(m.label == "Quote" and m.value.startswith("Q-RFQ-2026-0104") for m in q.metric)
    assert len(q.get("download_button")) == 2


def test_clarification_run_review_and_quote_pages(svc):
    svc.start(sample("0103"), "reference")
    at = page("review")
    at.session_state["rfq_id"] = "RFQ-2026-0103"
    at.run()
    assert not at.exception, at.exception
    assert "blocker" in _texts(at).lower()
    q = page("quote")
    q.session_state["rfq_id"] = "RFQ-2026-0103"
    q.run()
    assert not q.exception and "Clarification e-mail" in _texts(q)


def test_knowledge_and_metrics_pages(svc):
    svc.start(sample("0102"), "reference", auto_approve=True)
    svc.start(sample("0101"), "reference")
    kb = page("knowledge").run()
    assert not kb.exception and any("`" in m.value and "#" in m.value for m in kb.markdown)
    m = page("metrics").run()
    assert not m.exception, m.exception
    assert any(x.label == "Triaged RFQs" and x.value == "2" for x in m.metric)
    assert any(x.label == "Time saved (est.)" for x in m.metric)


# ---------- pure helpers ----------


def test_routing_edits_from_editor_table(svc):
    v = svc.start(sample("0101"), "reference")
    ln = v.draft.lines[0]
    df = routing_frame(ln.routing)
    grind = df.index[df["op_code"] == "GRIND"][0]
    df.loc[grind, "cycle_min"] = 8.0
    sugg = next(op for op in ln.routing if op.suggested)
    df.loc[df["seq"] == sugg.seq, "accept"] = True
    df = df[df["op_code"] != "WASH_PACK"]
    df = pd.concat(
        [df, pd.DataFrame([{"op_code": "deburr_manual", "work_center": "deburr", "cycle_min": 0.5}])]
    )
    edits = routing_edits(1, ln.routing, df)
    kinds = {(e.field, e.seq) for e in edits}
    gseq = next(op.seq for op in ln.routing if op.op_code == "GRIND")
    wseq = next(op.seq for op in ln.routing if op.op_code == "WASH_PACK")
    assert ("cycle_min", gseq) in kinds and ("accept_suggested", sugg.seq) in kinds
    assert ("remove_op", wseq) in kinds
    add = next(e for e in edits if e.field == "add_op")
    assert add.new_value["op_code"] == "DEBURR_MANUAL" and add.new_value["work_center"] == "DEBURR"
    assert routing_edits(1, ln.routing, routing_frame(ln.routing)) == []  # untouched table → no edits

    v2 = svc.review(
        "RFQ-2026-0101", {"action": "edit", "reviewer": "t", "edits": [e.model_dump() for e in edits]}
    )
    op = next(o for o in v2.draft.lines[0].routing if o.seq == gseq)
    assert op.cycle_min == 8.0 and op.basis == "manual"
    assert not price_changes(svc.feedback("RFQ-2026-0101")[-1].diff).empty


def test_business_view_uses_worst_line_tier():
    runs = [
        {"rfq_id": "a", "tiers": {1: "fast_track"}},
        {"rfq_id": "b", "tiers": {1: "fast_track", 2: "manual"}},
        {"rfq_id": "c", "tiers": {}},
    ]
    b = business_view(runs)
    assert b["rfqs"] == 2 and b["minutes"] == 30 + 240 and b["baseline_minutes"] == 480
    assert b["saved_minutes"] == 210 and b["not_counted"] == ["c"]
