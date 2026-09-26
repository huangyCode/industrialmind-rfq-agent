"""Quote Writer (DESIGN §5.11): approved draft → Quote (HTML + JSON), cover letter, clarification e-mail.

Division of labour: every number (prices, quantities, lead times, dates) is rendered by the Jinja templates
from the costing results. The LLM only writes prose in the customer's language around a placeholder where
the template inserts the figures / the rule-generated question list. Its output is post-checked (placeholder
present exactly once, no currency amounts, no extra list items); any failure falls back to template text.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from rfq_agent.config import BIZ, QUOTE_DIR, TEMPLATE_DIR
from rfq_agent.models import (
    LineQuoteDraft,
    Quote,
    QuoteDraft,
    QuoteLine,
    QuoteLinePrice,
    RFQRequest,
    Severity,
    ShapeClass,
    ValidationIssue,
)

COMPANY = "PrecisionMotion GmbH"
PROMPT_VERSION = "v1"
PRICE_PLACEHOLDER = "[[PRICE_SUMMARY]]"
QUESTIONS_PLACEHOLDER = "[[QUESTIONS]]"
# quoting_policy.md#quantity-tiers: if only one quantity is requested, offer the next standard tier
STANDARD_TIERS = (50, 200, 500, 1000, 2500)
LANG_NAMES = {"de": "German", "en": "English"}

# € 1.234,56 | 1,234.56 EUR | 12 Euro | EUR 99 ... — any currency amount in LLM prose triggers the fallback
CURRENCY_RE = re.compile(
    r"(?:€|\bEUR\b|\bEuro?s?\b|\$|£|\bUSD\b|\bPLN\b|\bCNY\b|\bRMB\b)\s*\d|\d[\d.,\s]*\s*(?:€|\bEUR\b|\bEuros?\b|\$|£|\bUSD\b|\bPLN\b|\bCNY\b|\bRMB\b)",
    re.I,
)
LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+\S", re.M)

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html.j2", "html"], default_for_string=False),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def lang_of(request: RFQRequest) -> str:
    return request.language if request.language in ("de", "en") else "en"


def money(x: float, lang: str = "en") -> str:
    s = f"{x:,.2f}"
    if lang == "de":
        s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{s} EUR"


def num(x: float | int, lang: str = "en") -> str:
    s = f"{x:,}" if isinstance(x, int) else f"{x:,.2f}"
    return s.replace(",", ".") if lang == "de" else s


def fmt_date(d: date, lang: str = "en") -> str:
    return d.strftime("%d.%m.%Y") if lang == "de" else d.strftime("%d %b %Y")


_env.filters.update(money=money, num=num, fmt_date=fmt_date)


def offer_quantities(quantities: list[int]) -> tuple[list[int], set[int]]:
    """Quantities to price and which of them are optional extra tiers (quoting policy)."""
    qtys = sorted({q for q in quantities if q > 0})
    options: set[int] = set()
    if len(qtys) == 1:
        nxt = next((t for t in STANDARD_TIERS if t > qtys[0]), None)
        if nxt:
            qtys.append(nxt)
            options.add(nxt)
    return qtys, options


# ---------- assumptions ----------

_A = {
    "en": {
        "prices": "Prices in EUR per piece, net, excluding VAT; delivery terms {inc}.",
        "validity": "This quotation is valid for {days} days from the issue date.",
        "gen_tol": "{pn}: general tolerances per {tol} as stated on the drawing.",
        "gen_tol_missing": "{pn}: no general tolerance on the drawing; quoted on ISO 2768-m.",
        "material": "{pn}: material {mat} as per drawing, including {cert}.",
        "cert_22": "standard mill certificate (EN 10204 2.2)",
        "cert_31": "inspection certificate 3.1 per EN 10204 as requested",
        "ht": "{pn}: heat treatment '{ht}' by an approved subcontractor is included.",
        "surface": "{pn}: surface treatment '{st}' by an approved subcontractor is included.",
        "indexed": "{pn}: the material portion is indexed; it may be adjusted if the market price changes by more than 10 % before order.",
        "min_order": "{pn}: the minimum order value per line and delivery applies to the marked quantities.",
        "option": "{pn}: the additional quantity tier is offered as an option.",
        "unpriced": "{pn}: the assembly price is incomplete — {items} not included (price on request, drawing required).",
        "special": "Customer requirements noted: {reqs}. Items not covered by the listed operations are confirmed and priced at order entry.",
        "lead": "Lead times are from order date and are confirmed at order entry against current capacity.",
        "assembly": "{pn}: quoted as a complete assembly including assembly and functional test per parts list.",
    },
    "de": {
        "prices": "Preise in EUR pro Stück, netto, zzgl. MwSt.; Lieferbedingung {inc}.",
        "validity": "Dieses Angebot ist {days} Tage ab Ausstellungsdatum gültig.",
        "gen_tol": "{pn}: Allgemeintoleranzen nach {tol} gemäß Zeichnung.",
        "gen_tol_missing": "{pn}: keine Allgemeintoleranz auf der Zeichnung; Angebot auf Basis ISO 2768-m.",
        "material": "{pn}: Werkstoff {mat} gemäß Zeichnung, inkl. {cert}.",
        "cert_22": "Werkszeugnis (EN 10204 2.2)",
        "cert_31": "Abnahmeprüfzeugnis 3.1 nach EN 10204 wie angefragt",
        "ht": "{pn}: Wärmebehandlung „{ht}“ durch qualifizierten Unterlieferanten ist enthalten.",
        "surface": "{pn}: Oberflächenbehandlung „{st}“ durch qualifizierten Unterlieferanten ist enthalten.",
        "indexed": "{pn}: Der Materialanteil ist indexiert und kann bei Marktpreisänderungen über 10 % vor Bestellung angepasst werden.",
        "min_order": "{pn}: Für die gekennzeichneten Mengen gilt der Mindestauftragswert je Position und Lieferung.",
        "option": "{pn}: Die zusätzliche Mengenstaffel wird optional angeboten.",
        "unpriced": "{pn}: Der Baugruppenpreis ist unvollständig — {items} nicht enthalten (Preis auf Anfrage, Zeichnung erforderlich).",
        "special": "Kundenanforderungen vermerkt: {reqs}. Nicht durch die aufgeführten Arbeitsgänge abgedeckte Leistungen werden bei Auftragseingang bestätigt und bepreist.",
        "lead": "Lieferzeiten ab Bestelldatum; Bestätigung bei Auftragseingang gemäß aktueller Kapazität.",
        "assembly": "{pn}: Angebot als komplette Baugruppe inkl. Montage und Funktionsprüfung gemäß Stückliste.",
    },
}

INDEXED_GROUPS = ("stainless", "aluminium", "aluminum")


def unpriced_items(line: LineQuoteDraft) -> list[str]:
    return [
        f"{b.part_number or ''} {b.description}".strip()
        for b in line.bom
        if b.source in ("catalog", "internal", "new") and b.unit_cost_eur is None
    ]


def _wants_31(request: RFQRequest) -> bool:
    """Customer asked for an EN 10204 3.1 inspection certificate (instead of the standard 2.2)."""
    return any(re.search(r"\b3\.1\b", r) and "10204" in r for r in request.special_requirements)


def build_assumptions(
    request: RFQRequest, lines: list[LineQuoteDraft], materials: Any = None, options: dict[int, set] = None
) -> list[str]:
    """Customer-facing quotation assumptions in the customer's language (quoting_policy.md)."""
    lang = lang_of(request)
    t = _A[lang]
    out = [t["prices"].format(inc=request.incoterm or "FCA")]
    for ln in lines:
        spec = ln.spec
        pn = spec.title_block.part_number or ln.item.customer_part_number or f"#{ln.line_no}"
        if spec.envelope.shape_class == ShapeClass.ASSEMBLY:
            out.append(t["assembly"].format(pn=pn))
        else:
            tol = spec.title_block.general_tolerance
            out.append(t["gen_tol"].format(pn=pn, tol=tol) if tol else t["gen_tol_missing"].format(pn=pn))
            if spec.title_block.material:
                cert = t["cert_31"] if _wants_31(request) else t["cert_22"]
                out.append(t["material"].format(pn=pn, mat=spec.title_block.material, cert=cert))
            if spec.heat_treatment:
                out.append(t["ht"].format(pn=pn, ht=spec.heat_treatment))
            if spec.surface_treatment:
                out.append(t["surface"].format(pn=pn, st=spec.surface_treatment))
            code = spec.title_block.material_code
            if materials is not None and code and code in materials.all:
                if any(g in (materials.all[code].mat_group or "").lower() for g in INDEXED_GROUPS):
                    out.append(t["indexed"].format(pn=pn))
        plant = ln.recommended_plant
        if any(c.min_order_applied for c in ln.costs if c.plant == plant):
            out.append(t["min_order"].format(pn=pn))
        if options and options.get(ln.line_no):
            out.append(t["option"].format(pn=pn))
        if items := unpriced_items(ln):
            out.append(t["unpriced"].format(pn=pn, items=", ".join(items)))
    if request.special_requirements:
        out.append(t["special"].format(reqs="; ".join(request.special_requirements)))
    out.append(t["lead"])
    out.append(t["validity"].format(days=BIZ.quote_validity_days))
    return out


# ---------- quote ----------


def quote_lines(draft: QuoteDraft) -> list[QuoteLine]:
    out = []
    for ln in draft.lines:
        plant = ln.recommended_plant
        _, options = offer_quantities(ln.item.quantities)
        prices = [
            QuoteLinePrice(
                qty=c.qty,
                unit_price_eur=round(c.unit_price_eur, 2),
                total_price_eur=round(c.total_price_eur, 2),
                option=c.qty in options,
                min_order_applied=c.min_order_applied,
            )
            for c in sorted(ln.costs, key=lambda c: c.qty)
            if c.plant == plant and c.feasible
        ]
        primary = next(
            (c for c in ln.costs if c.plant == plant and c.qty == min(ln.item.quantities or [0])), None
        )
        lead = primary.lead_time_weeks if primary else max((c.lead_time_weeks for c in ln.costs), default=0)
        unpriced = unpriced_items(ln)
        if not prices:
            unpriced = ["complete line (no feasible plant)", *unpriced]
        out.append(
            QuoteLine(
                line_no=ln.line_no,
                part_number=ln.spec.title_block.part_number or ln.item.customer_part_number,
                customer_part_number=ln.item.customer_part_number,
                description=ln.spec.title_block.title or ln.item.description,
                plant=plant or "-",
                prices=prices,
                lead_time_weeks=lead,
                unpriced_items=unpriced,
            )
        )
    return out


def next_version(quote_repo: Any, rfq_id: str) -> int:
    if quote_repo is None:
        return 1
    return 1 + max((r["version"] for r in quote_repo.list() if r["rfq_id"] == rfq_id), default=0)


def price_summary(quote: Quote) -> str:
    return _env.get_template("price_summary.txt.j2").render(q=quote, lang=quote.language).strip()


def build_quote(
    draft: QuoteDraft,
    *,
    approved_by: str,
    version: int,
    today: date,
    comment: str | None = None,
    approved_at: datetime | None = None,
) -> Quote:
    req = draft.request
    return Quote(
        quote_id=f"Q-{draft.rfq_id}-v{version}",
        rfq_id=draft.rfq_id,
        customer_name=req.customer_name,
        contact_name=req.contact_name,
        language=lang_of(req),
        issued_on=today,
        valid_until=today + timedelta(days=BIZ.quote_validity_days),
        currency="EUR",
        incoterm=req.incoterm,
        lines=quote_lines(draft),
        assumptions=list(draft.assumptions),
        cover_letter="",
        approved_by=approved_by,
        approved_at=approved_at or datetime.now().replace(microsecond=0),
        version=version,
        review_comment=comment,
    )


# ---------- LLM prose with template fallback ----------

COVER_SYSTEM = """You write the body of a cover letter that accompanies a quotation from {company}, a German
precision machining supplier, to an industrial customer.

Rules:
- Write in {lang_name} only, in a polite, concise business tone. 5 to 8 sentences.
- Use only the facts given by the user. Do not invent facts.
- Do NOT write any prices, amounts, currencies, quantities, dates or lead times. The system inserts all
  figures itself: write the placeholder {placeholder} on its own line exactly once where the price overview
  belongs.
- Content: salutation to the contact person, thanks for the enquiry, reference to the attached quotation,
  the placeholder, one or two key assumptions in plain words, next steps (order or questions).
- Do not add a sign-off, name or signature; the system appends it.
Return only the letter text."""

CLARIFY_SYSTEM = """You write a short e-mail from {company}, a German precision machining supplier, to a
customer whose request for quotation cannot be priced yet because some points are open.

Rules:
- Write in {lang_name} only, polite and concise (3 to 5 sentences in total).
- Do NOT write, list, add, remove or rephrase the questions. The system inserts the numbered list of
  questions itself: write the placeholder {placeholder} on its own line exactly once where the list belongs.
- Do not write any prices, amounts or currencies.
- Content: salutation to the contact person, thanks for the enquiry, say that a few points must be clarified
  before a quotation can be prepared, the placeholder, ask for a reply and say the quotation follows promptly.
- Do not add a sign-off, name or signature; the system appends it.
Return only the e-mail text."""


def check_prose(text: str, placeholder: str, *, allow_list_items: bool = True) -> str | None:
    """Return a rejection reason, or None if the LLM text is usable."""
    if not text or len(text.strip()) < 40:
        return "empty or too short"
    if len(text) > 3000:
        return "too long"
    if text.count(placeholder) != 1:
        return f"placeholder {placeholder} not present exactly once"
    if CURRENCY_RE.search(text):
        return "contains a currency amount"
    if not allow_list_items and LIST_ITEM_RE.search(text.replace(placeholder, "")):
        return "added its own list items"
    return None


def _llm_prose(
    client: Any, *, task: str, system: str, user_text: str, placeholder: str, rfq_id: str, list_ok: bool
) -> tuple[str | None, str]:
    if client is None:
        return None, "template: no LLM client"
    try:
        text = client.text(
            task=task, prompt_version=PROMPT_VERSION, system=system, user_text=user_text, rfq_id=rfq_id
        )
    except Exception as e:  # model down, cache miss, provider error → template
        return None, f"template: LLM unavailable ({type(e).__name__})"
    text = (text or "").strip()
    reason = check_prose(text, placeholder, allow_list_items=list_ok)
    if reason:
        return None, f"template: LLM text rejected ({reason})"
    return text, "llm"


def _signature(lang: str) -> str:
    return _env.get_template("signature.txt.j2").render(lang=lang, company=COMPANY).strip()


def cover_letter(quote: Quote, draft: QuoteDraft, client: Any = None) -> tuple[str, str]:
    """Return (letter text incl. price summary and signature, source)."""
    lang = quote.language
    summary = price_summary(quote)
    facts = {
        "customer_company": quote.customer_name,
        "contact_person": quote.contact_name,
        "our_reference": quote.quote_id,
        "customer_rfq": quote.rfq_id,
        "parts": [f"{ln.part_number} {ln.description or ''}".strip() for ln in quote.lines],
        "key_assumptions": [a for a in quote.assumptions if "EUR" not in a][:3],
        "price_on_request_items": [i for ln in quote.lines for i in ln.unpriced_items],
    }
    user = "Facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, indent=2)
    system = COVER_SYSTEM.format(company=COMPANY, lang_name=LANG_NAMES[lang], placeholder=PRICE_PLACEHOLDER)
    body, source = _llm_prose(
        client,
        task="cover_letter",
        system=system,
        user_text=user,
        placeholder=PRICE_PLACEHOLDER,
        rfq_id=quote.rfq_id,
        list_ok=True,
    )
    if body is None:
        body = _env.get_template("cover_letter.txt.j2").render(
            q=quote, lang=lang, placeholder=PRICE_PLACEHOLDER
        )
    body = body.strip().replace(PRICE_PLACEHOLDER, summary)
    return f"{body}\n\n{_signature(lang)}", source


# ---------- clarification ----------

_ASK = {
    "VAL-010": (
        "Please send us the drawing for this item.",
        "Bitte senden Sie uns die Zeichnung zu dieser Position.",
    ),
    "VAL-001": (
        "Please state the material designation and standard (e.g. 42CrMo4+QT, EN 10083-3).",
        "Bitte nennen Sie Werkstoff und Norm (z. B. 42CrMo4+QT, EN 10083-3).",
    ),
    "VAL-002": (
        "Please confirm the material designation and standard.",
        "Bitte bestätigen Sie Werkstoffbezeichnung und Norm.",
    ),
    "VAL-003": (
        "Please let us know the required quantities or the annual demand.",
        "Bitte teilen Sie uns die benötigten Stückzahlen bzw. den Jahresbedarf mit.",
    ),
    "VAL-004": (
        "May we quote on general tolerances ISO 2768-m?",
        "Dürfen wir auf Basis der Allgemeintoleranz ISO 2768-m anbieten?",
    ),
    "VAL-011": (
        "Please send us the drawing for this part; until then it is quoted as price on request.",
        "Bitte senden Sie uns die Zeichnung zu diesem Teil; bis dahin führen wir es als „Preis auf Anfrage“.",
    ),
    "DFM-001": (
        "Is this wall thickness functionally required, or could it be increased? Thin walls cause distortion and raise cost.",
        "Ist diese Wandstärke funktional erforderlich oder kann sie erhöht werden? Dünne Wände verursachen Verzug und Mehrkosten.",
    ),
    "DFM-002": (
        "Could the hole depth be reduced or the diameter increased? Otherwise deep-hole drilling is required, which increases cost.",
        "Kann die Bohrungstiefe reduziert oder der Durchmesser vergrößert werden? Andernfalls ist Tiefbohren mit Mehrkosten erforderlich.",
    ),
    "DFM-004": (
        "Is this surface roughness functionally required? It needs an additional finishing operation.",
        "Ist diese Rauheit funktional erforderlich? Sie erfordert einen zusätzlichen Feinbearbeitungsgang.",
    ),
    "DFM-006": (
        "May we machine internal corners with a radius of at least 0.5 mm?",
        "Dürfen wir Innenecken mit einem Radius von mindestens 0,5 mm ausführen?",
    ),
}
_ASK_DEFAULT = ("Please confirm or advise.", "Bitte um kurze Bestätigung bzw. Rückmeldung.")


def _customer_subject(message: str) -> tuple[str, bool]:
    """Customer-facing reference of a rule finding, without internal feature ids, thresholds or notes.

    'F4: wall 1.5 mm < 2.0 mm minimum (unknown material, steel limit used).' -> ('wall 1.5 mm', True)
    "Parts list item 6 'EC-5103' is neither ..."                               -> ('EC-5103', True)
    'The material field in the title block is empty.'                         -> (that sentence, False)
    """
    quoted = re.search(r"'([^']+)'", message)
    if not re.match(r"^F\d+:\s*", message) and quoted:
        return quoted.group(1), True
    is_ref = bool(re.match(r"^F\d+:\s*", message))
    s = re.sub(r"^F\d+:\s*", "", message)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.split(r"\s[<>≤≥]\s|,\s", s)[0].strip().rstrip(".")
    return s, is_ref


def customer_questions(
    request: RFQRequest, issues: list[ValidationIssue], comment: str | None = None
) -> list[str]:
    """One question per BLOCKER / WARNING issue (rule output, never invented), plus the reviewer's comment."""
    lang = lang_of(request)
    items = {it.line_no: it for it in request.items}
    out = []
    for i in issues:
        if i.severity not in (Severity.BLOCKER, Severity.WARNING):
            continue
        item = items.get(i.line_no)
        pn = (item.customer_part_number or item.drawing_ref) if item else f"#{i.line_no}"
        en, de = _ASK.get(i.code, _ASK_DEFAULT)
        subject, is_ref = _customer_subject(i.message)
        ask = de if lang == "de" else en
        if is_ref:
            out.append(f"{pn} ({subject}): {ask}")
        else:
            out.append(f"{pn}: {ask}" if lang == "de" else f"{pn}: {subject}. {ask}")
    if comment and comment.strip():
        out.append(comment.strip())
    return out


def clarification_email(
    request: RFQRequest,
    issues: list[ValidationIssue],
    client: Any = None,
    *,
    comment: str | None = None,
) -> dict:
    """Clarification e-mail in the customer's language. Questions come from the rules only."""
    lang = lang_of(request)
    questions = customer_questions(request, issues, comment)
    parts = [it.customer_part_number or it.drawing_ref for it in request.items]
    subject = (
        _env.get_template("clarification_subject.txt.j2")
        .render(lang=lang, rfq_id=request.rfq_id, parts=", ".join(parts))
        .strip()
    )
    facts = {
        "customer_company": request.customer_name,
        "contact_person": request.contact_name,
        "customer_rfq": request.rfq_id,
        "parts": parts,
        "number_of_open_points": "several" if len(questions) > 1 else "one",
    }
    system = CLARIFY_SYSTEM.format(
        company=COMPANY, lang_name=LANG_NAMES[lang], placeholder=QUESTIONS_PLACEHOLDER
    )
    body, source = _llm_prose(
        client,
        task="clarification_email",
        system=system,
        user_text="Facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, indent=2),
        placeholder=QUESTIONS_PLACEHOLDER,
        rfq_id=request.rfq_id,
        list_ok=False,
    )
    if body is None:
        body = _env.get_template("clarification.txt.j2").render(
            lang=lang, contact=request.contact_name, placeholder=QUESTIONS_PLACEHOLDER
        )
    qtext = "\n".join(f"{n}. {q}" for n, q in enumerate(questions, 1))
    body = body.strip().replace(QUESTIONS_PLACEHOLDER, qtext)
    return {
        "to": request.contact_email,
        "subject": subject,
        "body": f"{body}\n\n{_signature(lang)}",
        "questions": questions,
        "language": lang,
        "source": source,
    }


# ---------- output ----------


def render_html(quote: Quote) -> str:
    return _env.get_template("quote.html.j2").render(q=quote, lang=quote.language, company=COMPANY)


def write_quote(
    draft: QuoteDraft,
    *,
    approved_by: str,
    client: Any = None,
    quote_repo: Any = None,
    quote_dir: Path = QUOTE_DIR,
    today: date | None = None,
    comment: str | None = None,
) -> tuple[Quote, dict[str, str]]:
    """Build the Quote, write the cover letter, save HTML + JSON to `quote_dir` and the quote table."""
    version = next_version(quote_repo, draft.rfq_id)
    quote = build_quote(
        draft, approved_by=approved_by, version=version, today=today or date.today(), comment=comment
    )
    quote.cover_letter, quote.cover_letter_source = cover_letter(quote, draft, client)
    quote_dir = Path(quote_dir)
    quote_dir.mkdir(parents=True, exist_ok=True)
    html_path = quote_dir / f"{quote.quote_id}.html"
    json_path = quote_dir / f"{quote.quote_id}.json"
    html_path.write_text(render_html(quote), encoding="utf-8")
    json_path.write_text(quote.model_dump_json(indent=2), encoding="utf-8")
    if quote_repo is not None:
        quote_repo.save(
            quote.quote_id,
            quote.rfq_id,
            version,
            "approved",
            quote.model_dump_json(),
            approved_by,
            quote.approved_at.isoformat(),
        )
    return quote, {"html": str(html_path), "json": str(json_path)}
