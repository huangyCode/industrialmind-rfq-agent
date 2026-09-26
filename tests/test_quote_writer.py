"""Quote writer: number formatting, LLM prose post-checks, questions from rules, template fallbacks."""

from __future__ import annotations

from rfq_agent.agents import quote_writer as qw
from rfq_agent.config import SAMPLES_DIR
from rfq_agent.models import RFQRequest, Severity, ValidationIssue


def _req(n: str) -> RFQRequest:
    return RFQRequest.model_validate_json(
        (SAMPLES_DIR / f"RFQ-2026-{n}" / "expected" / "rfq.json").read_text(encoding="utf-8")
    )


def _issue(code: str, sev: Severity, msg: str = "msg") -> ValidationIssue:
    return ValidationIssue(code=code, severity=sev, line_no=1, message=msg, suggestion="s")


def test_money_and_numbers():
    assert qw.money(1234.5, "en") == "1,234.50 EUR"
    assert qw.money(1234.5, "de") == "1.234,50 EUR"
    assert qw.num(2500, "de") == "2.500" and qw.num(2500, "en") == "2,500"


def test_offer_quantities_adds_next_standard_tier_only_for_single_qty():
    assert qw.offer_quantities([1000]) == ([1000, 2500], {2500})
    assert qw.offer_quantities([300]) == ([300, 500], {500})
    assert qw.offer_quantities([200, 500]) == ([200, 500], set())
    assert qw.offer_quantities([5000]) == ([5000], set())


def test_check_prose():
    ok = "Dear Ms X,\n\nthank you for your enquiry.\n\n[[QUESTIONS]]\n\nBest wishes from us."
    assert qw.check_prose(ok, "[[QUESTIONS]]", allow_list_items=False) is None
    for bad, why in [
        (ok.replace("[[QUESTIONS]]", ""), "placeholder"),
        (ok + "\n[[QUESTIONS]]", "placeholder"),
        (ok + " Total 1.234,00 €.", "currency"),
        (ok + " Price EUR 12.", "currency"),
        (ok + " about 99 Euro", "currency"),
        (ok + "\n1. What material?", "list items"),
        ("short [[QUESTIONS]]", "short"),
    ]:
        assert why in (qw.check_prose(bad, "[[QUESTIONS]]", allow_list_items=False) or ""), bad
    # non-currency numbers (identifiers, standards) are fine
    assert qw.check_prose(ok + " Ref Q-RFQ-2026-0101-v1, ISO 2768-m.", "[[QUESTIONS]]") is None


def test_questions_come_from_blockers_and_warnings_only():
    issues = [
        _issue("VAL-001", Severity.BLOCKER, "Material empty."),
        _issue("DFM-003", Severity.INFO),
        _issue("DFM-002", Severity.WARNING, "Deep hole."),
        _issue("XYZ-999", Severity.WARNING, "Something."),
    ]
    en = qw.customer_questions(_req("0103"), issues)
    assert len(en) == 3 and en[0].startswith("BR-0930: Material empty.")
    assert en[2].endswith("Please confirm or advise.")
    de = qw.customer_questions(_req("0101"), issues, comment="Bitte Losgröße bestätigen.")
    assert len(de) == 4 and "Werkstoff" in de[0] and de[-1] == "Bitte Losgröße bestätigen."


def test_clarification_template_fallback_in_both_languages():
    issues = [_issue("VAL-001", Severity.BLOCKER, "Material empty.")]
    en = qw.clarification_email(_req("0103"), issues, client=None)
    assert en["source"] == "template: no LLM client" and en["language"] == "en"
    assert en["body"].startswith("Dear Sanne Verbeek,") and "1. BR-0930" in en["body"]
    assert en["subject"].startswith("Re: Your request for quotation RFQ-2026-0103")
    de = qw.clarification_email(_req("0101"), issues, client=None)
    assert de["body"].startswith("Sehr geehrte/r Jonas Brückner,") and "Mit freundlichen Grüßen" in de["body"]
    assert de["subject"].startswith("AW: Ihre Anfrage RFQ-2026-0101")


def test_clarification_llm_may_not_add_questions():
    class Chatty:
        def text(self, **kw):
            return (
                "Dear Sanne,\n\nplease answer:\n\n[[QUESTIONS]]\n4. Also, what colour do you want?\n\nThanks"
            )

    mail = qw.clarification_email(_req("0103"), [_issue("VAL-001", Severity.BLOCKER)], client=Chatty())
    assert mail["source"] == "template: LLM text rejected (added its own list items)"
    assert "colour" not in mail["body"]


def test_llm_prompt_contains_no_prices():
    seen = {}

    class Spy:
        def text(self, **kw):
            seen.update(kw)
            return "Dear Ms X,\n\nthank you for your enquiry and trust.\n\n[[PRICE_SUMMARY]]\n\nKind regards"

    from datetime import date, datetime

    from rfq_agent.models import Quote, QuoteLine, QuoteLinePrice

    q = Quote(
        quote_id="Q-RFQ-X-v1",
        rfq_id="RFQ-X",
        customer_name="C",
        contact_name="X",
        language="en",
        issued_on=date(2026, 9, 26),
        valid_until=date(2026, 10, 26),
        currency="EUR",
        incoterm="FCA",
        lines=[
            QuoteLine(
                line_no=1,
                part_number="P-1",
                description="Part",
                plant="DE",
                prices=[QuoteLinePrice(qty=10, unit_price_eur=77.77, total_price_eur=777.7)],
                lead_time_weeks=3,
            )
        ],
        assumptions=["Prices in EUR per piece, net.", "Material as per drawing."],
        cover_letter="",
        approved_by="me",
        approved_at=datetime(2026, 9, 26, 12, 0),
    )
    text, source = qw.cover_letter(q, None, Spy())
    assert source == "llm" and "77.77" not in seen["user_text"] and "77.77 EUR" in text
    assert text.rstrip().endswith("PrecisionMotion GmbH")
    html = qw.render_html(q.model_copy(update={"cover_letter": text}))
    assert "77.77 EUR" in html and "777.70 EUR" in html and "Q-RFQ-X-v1" in html
