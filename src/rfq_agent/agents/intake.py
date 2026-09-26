"""Intake Agent: RFQ email -> RFQRequest (DESIGN §5.1)."""

from __future__ import annotations

import re
from pathlib import Path

from rfq_agent.llm.client import LLMClient, load_prompt
from rfq_agent.models import RFQRequest

PROMPT_VERSION = "v3"
DRAWING_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")


def list_attachments(rfq_dir: Path) -> list[str]:
    d = Path(rfq_dir) / "drawings"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.suffix.lower() in DRAWING_EXT)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", Path(name).stem.lower())


def match_attachment(ref: str | None, attachments: list[str]) -> str | None:
    """Fuzzy-match a drawing reference (case / extension / separators) to an attachment file name."""
    if not ref:
        return None
    n = _norm(ref)
    for a in attachments:
        if _norm(a) == n:
            return a
    hits = [a for a in attachments if n and (_norm(a) in n or n in _norm(a))]
    return hits[0] if len(hits) == 1 else None


def build_user_text(rfq_id: str, email: str, attachments: list[str]) -> str:
    att = "\n".join(f"- {a}" for a in attachments) or "- (none)"
    return (
        f"RFQ id: {rfq_id}\n\nAttachments:\n{att}\n\n--- EMAIL START ---\n{email.strip()}\n--- EMAIL END ---"
    )


def normalize_request(req: RFQRequest, rfq_id: str, attachments: list[str]) -> RFQRequest:
    """Deterministic clean-up after the model: id, attachment names, quantity order, line numbers."""
    req = req.model_copy(deep=True)
    req.rfq_id = rfq_id
    req.currency = (req.currency or "EUR").upper()
    for f in ("contact_name", "contact_email", "incoterm"):
        if not (getattr(req, f) or "").strip():
            setattr(req, f, None)
    if req.incoterm:
        req.incoterm = req.incoterm.strip().upper().split()[0]
        # delivery terms are not special requirements; small models tend to repeat them there
        inco = re.compile(rf"\b{re.escape(req.incoterm)}\b", re.I)
        req.special_requirements = [r for r in req.special_requirements if not inco.search(r)]
    for i, item in enumerate(req.items, start=1):
        item.line_no = i
        item.notes = (item.notes or "").strip() or None
        item.quantities = sorted({q for q in item.quantities if q > 0})
        # unmatched refs are kept as written; Review raises VAL-010 for them
        item.drawing_ref = match_attachment(item.drawing_ref, attachments) or item.drawing_ref
    return req


def parse_rfq(rfq_dir: Path, client: LLMClient) -> RFQRequest:
    """Read email.txt + attachment names from an RFQ folder and extract an RFQRequest."""
    rfq_dir = Path(rfq_dir)
    rfq_id = rfq_dir.name
    email = (rfq_dir / "email.txt").read_text(encoding="utf-8")
    attachments = list_attachments(rfq_dir)
    req = client.structured(
        task="intake",
        prompt_version=PROMPT_VERSION,
        system=load_prompt("intake", PROMPT_VERSION),
        user_text=build_user_text(rfq_id, email, attachments),
        schema=RFQRequest,
        rfq_id=rfq_id,
        max_tokens=4096,
    )
    return normalize_request(req, rfq_id, attachments)
