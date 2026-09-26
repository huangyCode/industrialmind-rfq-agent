"""End-to-end graph tests: reference extractor (gold data) + fake text LLM, isolated checkpoint / quote dir."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from rfq_agent.app_api import RFQService
from rfq_agent.config import BIZ, SAMPLES_DIR
from rfq_agent.graph import Deps, EditError, ReferenceExtractor
from rfq_agent.models import Quote, ReviewDecision, ReviewEdit, RFQStatus, Tier

TODAY = date(2026, 9, 26)
NODES_FAST = [
    "intake",
    "extract_drawings",
    "validate",
    "retrieve_similar",
    "plan_bom_routing",
    "cost",
    "assess",
    "human_review",
    "finalize",
]


class FakeLLM:
    """Text-only stand-in for LLMClient: canned prose with the required placeholder."""

    DEFAULTS = {
        "cover_letter": (
            "Dear customer,\n\nthank you for your enquiry. Please find our quotation below.\n\n"
            "[[PRICE_SUMMARY]]\n\nWe look forward to your order."
        ),
        "clarification_email": (
            "Dear customer,\n\nthank you for your enquiry. Before we can quote, please clarify:\n\n"
            "[[QUESTIONS]]\n\nWe will send the quotation as soon as we hear from you."
        ),
    }

    def __init__(self, responses: dict | None = None, fail: bool = False):
        self.responses = responses or {}
        self.fail = fail
        self.calls: list[dict] = []
        self.stats = {"llm_calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}

    def text(self, *, task, prompt_version, system, user_text, rfq_id=None, max_tokens=None):
        self.calls.append({"task": task, "system": system, "user_text": user_text})
        self.stats["llm_calls"] += 1
        self.stats["input_tokens"] += 100
        self.stats["output_tokens"] += 50
        if self.fail:
            raise RuntimeError("model down")
        return self.responses.get(task, self.DEFAULTS[task])


def make_service(db, tmp_path: Path, llm=None, cp_name: str = "cp.db") -> RFQService:
    deps = Deps.from_conn(
        db,
        client=llm if llm is not None else FakeLLM(),
        extractors={"reference": ReferenceExtractor()},
        quote_dir=tmp_path / "quotes",
        today=TODAY,
    )
    return RFQService(deps, tmp_path / cp_name)


def sample(n: str) -> Path:
    return SAMPLES_DIR / f"RFQ-2026-{n}"


@pytest.fixture
def svc(db, tmp_path):
    return make_service(db, tmp_path)


def _price(view, line_no: int, plant: str, qty: int) -> float:
    ln = next(ln for ln in view.draft.lines if ln.line_no == line_no)
    return next(c for c in ln.costs if c.plant == plant and c.qty == qty).unit_price_eur


# ---------- the four demo RFQs ----------


def test_0102_fast_track_auto_approve_writes_quote(svc, db):
    v = svc.start(sample("0102"), "reference", auto_approve=True)
    assert v.tiers == {1: Tier.FAST_TRACK}
    assert v.auto_approved and v.status == RFQStatus.APPROVED.value and not v.pending_review
    assert [e.node for e in v.trace] == NODES_FAST
    q = v.quote
    assert re.fullmatch(r"Q-RFQ-2026-0102-v\d+", q.quote_id)
    assert q.valid_until == TODAY.replace(day=26, month=10)  # 30 days
    assert q.currency == "EUR" and q.incoterm == "FCA" and q.language == "en"
    line = q.lines[0]
    assert line.part_number == "FL-2208" and line.plant == v.draft.lines[0].recommended_plant
    assert [p.qty for p in line.prices] == [1000, 2500] and line.prices[1].option  # next standard tier
    assert line.prices[0].unit_price_eur == round(_price(v, 1, line.plant, 1000), 2)
    html = Path(v.quote_paths["html"]).read_text(encoding="utf-8")
    assert q.quote_id in html and f"{line.prices[0].unit_price_eur:,.2f} EUR" in html
    assert Quote.model_validate_json(Path(v.quote_paths["json"]).read_text()) == q
    assert any(r["quote_id"] == q.quote_id for r in svc.deps.quotes.list())
    # prose from the (fake) LLM, figures from the template
    assert q.cover_letter_source == "llm"
    assert "[[PRICE_SUMMARY]]" not in q.cover_letter and "1,000 pcs" in q.cover_letter
    assert "FL-2150" not in html  # reference part stays internal (quoting_policy.md#part-reuse)
    fin = next(e for e in v.trace if e.node == "finalize")
    assert fin.llm_calls == 1 and fin.input_tokens == 100


def test_0103_blocker_goes_to_clarification_without_price(svc):
    llm = svc.deps.client
    v = svc.start(sample("0103"), "reference", auto_approve=True)
    assert v.status == RFQStatus.NEEDS_CLARIFICATION.value
    assert v.next_nodes == [] and not v.pending_review
    assert v.draft is None and v.quote is None
    assert [e.node for e in v.trace] == ["intake", "extract_drawings", "validate", "clarify"]
    c = v.clarification
    assert c["language"] == "en" and c["source"] == "llm"
    assert c["to"] == "s.verbeek@tervalo-automation.example"
    assert len(c["questions"]) == 3  # VAL-001 blocker + DFM-001 + DFM-002 warnings, nothing else
    assert "material" in c["questions"][0].lower()
    assert all(f"{n}. " in c["body"] for n in (1, 2, 3))
    assert "EUR" not in c["body"] and "€" not in c["body"]
    assert [x["task"] for x in llm.calls] == ["clarification_email"]


def test_0101_edit_grind_cycle_recalculates_and_records_feedback(svc):
    v = svc.start(sample("0101"), "reference", auto_approve=True)
    assert v.tiers == {1: Tier.STANDARD}
    assert v.pending_review and not v.auto_approved  # --auto-approve never approves STANDARD
    ln = v.draft.lines[0]
    cn = [c for c in ln.costs if c.plant == "CN"]
    assert cn and not any(c.feasible for c in cn) and "GRIND_CYL" in cn[0].infeasible_reason
    grind = next(op for op in ln.routing if op.op_code == "GRIND")
    plant = ln.recommended_plant
    before = {q: _price(v, 1, plant, q) for q in (200, 500)}
    n_feedback = len(svc.feedback("RFQ-2026-0101"))

    new_cycle = grind.cycle_min + 0.8
    v2 = svc.review(
        "RFQ-2026-0101",
        ReviewDecision(
            action="edit",
            reviewer="M. Weber",
            edits=[
                ReviewEdit(line_no=1, target="routing", seq=grind.seq, field="cycle_min", new_value=new_cycle)
            ],
        ),
    )
    assert v2.pending_review and v2.review_round == 1 and v2.draft.version == 2
    op2 = next(op for op in v2.draft.lines[0].routing if op.seq == grind.seq)
    assert op2.cycle_min == new_cycle and op2.basis == "manual"
    # unit price moves by Δcycle × rate × (1 + overhead + logistics) / (1 − margin), setup unchanged
    p = svc.deps.rates.plants[plant]
    rate = svc.deps.rates.rate(plant, grind.work_center)
    expected = 0.8 / 60 * rate * (1 + p["overhead_pct"] + p["logistics_pct"]) / (1 - BIZ.margin_pct)
    for q in (200, 500):
        assert _price(v2, 1, plant, q) - before[q] == pytest.approx(expected, rel=1e-6)
    assert [e.node for e in v2.trace][-3:] == ["human_review", "cost", "assess"]
    assert "feedback recorded" in v2.trace[-1].summary

    fb = svc.feedback("RFQ-2026-0101")
    assert len(fb) == n_feedback + 1
    rec = fb[-1]
    assert rec.reviewer == "M. Weber" and rec.edits[0].field == "cycle_min"
    paths = {d["path"]: d for d in rec.diff}
    key = f"routing[seq={grind.seq},GRIND].cycle_min"
    assert paths[key]["before"] == grind.cycle_min and paths[key]["after"] == new_cycle
    assert f"unit_price[{plant}@200]" in paths

    v3 = svc.review("RFQ-2026-0101", ReviewDecision(action="approve", reviewer="M. Weber"))
    assert v3.status == RFQStatus.APPROVED.value and v3.quote.approved_by == "M. Weber"
    assert v3.quote.language == "de"
    assert v3.quote.lines[0].prices[0].unit_price_eur == round(_price(v2, 1, plant, 200), 2)
    assert [r["action"] for r in v3.reviews] == ["edit", "approve"]


def test_0104_assembly_new_part_price_on_request(svc):
    v = svc.start(sample("0104"), "reference", auto_approve=True)
    assert v.tiers == {1: Tier.STANDARD} and v.pending_review
    v = svc.review("RFQ-2026-0104", ReviewDecision(action="approve", reviewer="A. Krüger"))
    line = v.quote.lines[0]
    assert any("EC-5103" in u for u in line.unpriced_items)
    assert line.prices  # the rest of the assembly is still quoted
    assert any("EC-5103" in a and "unvollständig" in a for a in v.quote.assumptions)
    html = Path(v.quote_paths["html"]).read_text(encoding="utf-8")
    assert "Preis auf Anfrage" in html and "EC-5103" in html


# ---------- HITL mechanics ----------


def test_pending_review_survives_restart(db, tmp_path):
    svc1 = make_service(db, tmp_path)
    svc1.start(sample("0101"), "reference")
    del svc1  # simulated process exit
    svc2 = make_service(db, tmp_path)  # new deps, new graph object, same checkpoint DB
    v = svc2.get("RFQ-2026-0101")
    assert v.pending_review and v.draft is not None and v.tiers == {1: Tier.STANDARD}
    v = svc2.review("RFQ-2026-0101", ReviewDecision(action="approve", reviewer="night shift"))
    assert v.status == RFQStatus.APPROVED.value and Path(v.quote_paths["html"]).exists()
    assert any(r["rfq_id"] == "RFQ-2026-0101" and r["status"] == "approved" for r in svc2.list_runs())


def test_margin_and_plant_edits(svc):
    v = svc.start(sample("0101"), "reference")
    plant = v.draft.lines[0].recommended_plant
    p0 = _price(v, 1, "DE", 200)
    v = svc.review(
        "RFQ-2026-0101",
        ReviewDecision(
            action="edit",
            reviewer="x",
            edits=[
                ReviewEdit(line_no=1, target="margin", field="margin_pct", new_value=25),
                ReviewEdit(line_no=1, target="plant", field="plant", new_value="DE"),
            ],
        ),
    )
    assert v.draft.margin_pct == pytest.approx(0.25)
    assert _price(v, 1, "DE", 200) == pytest.approx(p0 * (1 - BIZ.margin_pct) / 0.75)
    assert v.draft.lines[0].recommended_plant == "DE"
    assert v.lines[0]["plant_overridden"] is True and plant == "PL"
    with pytest.raises(EditError, match="not feasible"):
        svc.review(
            "RFQ-2026-0101",
            ReviewDecision(
                action="edit",
                reviewer="x",
                edits=[ReviewEdit(line_no=1, target="plant", field="plant", new_value="CN")],
            ),
        )
    assert svc.get("RFQ-2026-0101").pending_review  # invalid edit did not move the graph


def test_accept_suggested_and_remove_op(svc):
    v = svc.start(sample("0101"), "reference")
    ln = v.draft.lines[0]
    sugg = next(op for op in ln.routing if op.suggested and not op.outsourced)
    wash = next(op for op in ln.routing if op.op_code == "WASH_PACK")
    p0 = _price(v, 1, "DE", 200)
    v = svc.review(
        "RFQ-2026-0101",
        ReviewDecision(
            action="edit",
            reviewer="x",
            edits=[
                ReviewEdit(line_no=1, target="routing", seq=sugg.seq, field="accept_suggested"),
                ReviewEdit(line_no=1, target="routing", seq=wash.seq, field="remove_op"),
            ],
        ),
    )
    routing = {op.seq: op for op in v.draft.lines[0].routing}
    assert not routing[sugg.seq].suggested and wash.seq not in routing
    assert _price(v, 1, "DE", 200) != p0


def test_reject_and_request_clarification(svc):
    svc.start(sample("0101"), "reference")
    v = svc.review("RFQ-2026-0101", ReviewDecision(action="reject", reviewer="x", comment="not our business"))
    assert v.status == RFQStatus.REJECTED.value and v.quote is None and not v.pending_review

    svc.start(sample("0104"), "reference")
    v = svc.review(
        "RFQ-2026-0104",
        ReviewDecision(
            action="request_clarification", reviewer="x", comment="Bitte Prüfspezifikation senden."
        ),
    )
    c = v.clarification
    assert v.status == RFQStatus.NEEDS_CLARIFICATION.value and c["language"] == "de"
    assert len(c["questions"]) == 2 and "EC-5103" in c["questions"][0]
    assert c["questions"][1] == "Bitte Prüfspezifikation senden."


def test_rerun_starts_fresh(svc):
    svc.start(sample("0102"), "reference", auto_approve=True)
    v = svc.start(sample("0102"), "reference")
    assert v.pending_review and v.quote is None
    assert [e.node for e in v.trace].count("intake") == 1


def test_llm_text_with_price_falls_back_to_template(db, tmp_path):
    llm = FakeLLM(
        responses={
            "cover_letter": "Dear Ms Hartwell,\n\nour price is EUR 23.50 per piece.\n\n[[PRICE_SUMMARY]]\n\nRegards"
        }
    )
    svc = make_service(db, tmp_path, llm=llm)
    v = svc.start(sample("0102"), "reference", auto_approve=True)
    q = v.quote
    assert q.cover_letter_source.startswith("template") and "currency" in q.cover_letter_source
    assert "23.50" not in q.cover_letter and "Dear Emily Hartwell" in q.cover_letter
    assert json.loads(Path(v.quote_paths["json"]).read_text())["cover_letter_source"] == q.cover_letter_source


def test_llm_down_uses_template_texts(db, tmp_path):
    svc = make_service(db, tmp_path, llm=FakeLLM(fail=True))
    v = svc.start(sample("0103"), "reference")
    assert v.clarification["source"].startswith("template: LLM unavailable")
    assert "Dear Sanne Verbeek" in v.clarification["body"] and "1. BR-0930" in v.clarification["body"]


def test_intake_error_ends_run(svc, tmp_path):
    d = tmp_path / "RFQ-2026-9999"
    (d / "drawings").mkdir(parents=True)
    (d / "email.txt").write_text("hello", encoding="utf-8")
    v = svc.start(d, "reference")
    assert v.status == RFQStatus.ERROR.value and "intake failed" in v.error and v.next_nodes == []


def test_unknown_extractor(svc):
    with pytest.raises(ValueError, match="Unknown extractor"):
        svc.start(sample("0102"), "gold")
