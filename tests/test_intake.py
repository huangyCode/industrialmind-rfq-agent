from __future__ import annotations

import json

from test_llm import FakeProvider

from rfq_agent.agents.intake import build_user_text, match_attachment, parse_rfq
from rfq_agent.llm.cache import LLMCache
from rfq_agent.llm.client import LLMClient

EMAIL = """From: Anna Beispiel <a.beispiel@example-antriebe.test>
Date: 2026-09-20
Subject: Anfrage Antriebswelle

Sehr geehrte Damen und Herren,
bitte bieten Sie uns die Welle SH-4711 Rev. B gemäß Zeichnung an: 50 / 200 / 500 Stück, Liefertermin 15.11.2026.
Materialzeugnis EN 10204 3.1 erforderlich. Lieferung DAP.
"""


def test_match_attachment():
    atts = ["SH-4711.pdf", "FL-2208.pdf"]
    assert match_attachment("sh-4711", atts) == "SH-4711.pdf"
    assert match_attachment("SH_4711.PDF", atts) == "SH-4711.pdf"
    assert match_attachment("Drawing SH-4711", atts) == "SH-4711.pdf"  # unique containment
    assert match_attachment("XX-0000.pdf", atts) is None
    assert match_attachment(None, atts) is None


def test_parse_rfq_with_fake_model(tmp_path):
    rfq = tmp_path / "RFQ-2026-0999"
    (rfq / "drawings").mkdir(parents=True)
    (rfq / "email.txt").write_text(EMAIL, encoding="utf-8")
    (rfq / "drawings" / "SH-4711.pdf").write_bytes(b"%PDF")
    answer = {
        "rfq_id": "whatever",
        "customer_name": "Example Antriebe",
        "contact_name": "Anna Beispiel",
        "contact_email": "a.beispiel@example-antriebe.test",
        "language": "de",
        "received_at": "2026-09-20",
        "incoterm": "dap",
        "currency": "eur",
        "special_requirements": ["EN 10204 3.1 material certificate", "Delivery DAP"],
        "items": [
            {
                "line_no": 3,
                "customer_part_number": "SH-4711",
                "drawing_ref": "sh-4711",
                "quantities": [500, 50, 200, 50],
                "requested_delivery": "2026-11-15",
            }
        ],
    }
    prov = FakeProvider([json.dumps(answer)])
    client = LLMClient(prov, mode="live", cache=LLMCache(tmp_path / "c"), failure_dir=tmp_path / "f")
    req = parse_rfq(rfq, client)
    assert req.rfq_id == "RFQ-2026-0999"
    assert req.special_requirements == ["EN 10204 3.1 material certificate"]
    assert req.incoterm == "DAP" and req.currency == "EUR" and req.language == "de"
    item = req.items[0]
    assert item.line_no == 1 and item.quantities == [50, 200, 500]
    assert item.drawing_ref == "SH-4711.pdf"
    sent = prov.calls[0]["messages"][0]["text"]
    assert sent == build_user_text("RFQ-2026-0999", EMAIL, ["SH-4711.pdf"])
    assert "- SH-4711.pdf" in sent and "RFQ id: RFQ-2026-0999" in sent
