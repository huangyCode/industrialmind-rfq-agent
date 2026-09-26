"""CLI smoke tests (reference extractor, fake text LLM, isolated service)."""

from __future__ import annotations

import pytest
from test_graph import make_service
from typer.testing import CliRunner

from rfq_agent import app_api
from rfq_agent.cli import app
from rfq_agent.config import SAMPLES_DIR

runner = CliRunner()


@pytest.fixture
def svc(db, tmp_path):
    s = make_service(db, tmp_path)
    app_api.set_service(s)
    yield s
    app_api.set_service(None)


def invoke(*args):
    r = runner.invoke(app, [str(a) for a in args], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output + repr(r.exception)
    return r.output


def test_run_fast_track_auto_approve(svc):
    out = invoke("run", SAMPLES_DIR / "RFQ-2026-0102", "--auto-approve", "--extractor", "reference")
    assert "FAST_TRACK" in out and "Q-RFQ-2026-0102-v" in out and "approved" in out
    assert "reference data" in out and "finalize" in out  # trace table


def test_run_review_edit_approve_show(svc):
    out = invoke("run", SAMPLES_DIR / "RFQ-2026-0101", "--auto-approve", "--extractor", "reference")
    assert "STANDARD" in out and "only applies to FAST_TRACK" in out and "GRIND_CYL not available" in out
    seq = next(op.seq for op in svc.get("RFQ-2026-0101").draft.lines[0].routing if op.op_code == "GRIND")
    out = invoke("review", "RFQ-2026-0101", "--edit-cycle", f"1:{seq}:9.5", "--reviewer", "M. Weber")
    assert "manual" in out and "Waiting for engineer review" in out
    out = invoke("review", "RFQ-2026-0101", "--approve", "--reviewer", "M. Weber")
    assert "approved by M. Weber" in out
    out = invoke("show", "RFQ-2026-0101")
    assert "Trace" in out and "human_review" in out
    assert "RFQ-2026-0101" in invoke("runs")


def test_run_clarification(svc):
    out = invoke("run", SAMPLES_DIR / "RFQ-2026-0103", "--extractor", "reference")
    assert "needs_clarification" in out and "no price issued" in out and "VAL-001" in out


def test_review_errors(svc):
    invoke("run", SAMPLES_DIR / "RFQ-2026-0101", "--extractor", "reference")
    r = runner.invoke(app, ["review", "RFQ-2026-0101", "--edit-cycle", "1:999:3"])
    assert r.exit_code == 1 and "no operation with seq 999" in r.output
    r = runner.invoke(app, ["review", "RFQ-2026-0101", "--approve", "--reject"])
    assert r.exit_code != 0


def test_kb_and_similar(svc):
    out = invoke("kb", "deep hole drilling")
    assert "dfm_guidelines.md#deep-holes" in out
    out = invoke("similar", SAMPLES_DIR / "RFQ-2026-0102" / "expected" / "FL-2208.json")
    assert "FL-2150" in out
