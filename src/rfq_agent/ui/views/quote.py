"""Quote: the approved quotation (HTML/JSON) and cover letter, or the clarification e-mail."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from rfq_agent.ui import common


def _clarification(c: dict) -> None:
    st.subheader("Clarification e-mail")
    st.caption(
        f"Language: {c.get('language')} · text source: {c.get('source')} · no price is sent until the open "
        "points are answered."
    )
    with st.container(border=True):
        st.markdown(f"**To:** {c.get('to') or '-'}  \n**Subject:** {c.get('subject')}")
        st.text(c.get("body", ""))
    if c.get("questions"):
        st.markdown("**Open points**")
        st.markdown("\n".join(f"{n}. {q}" for n, q in enumerate(c["questions"], 1)))
    st.download_button(
        "Download e-mail (.txt)",
        f"To: {c.get('to') or ''}\nSubject: {c.get('subject')}\n\n{c.get('body', '')}",
        file_name="clarification.txt",
        mime="text/plain",
        key="dl_clar",
    )


def render() -> None:
    st.title("Quote")
    try:
        svc = common.service()
        runs = [r for r in svc.list_runs() if r["quote_id"] or r["status"] == "needs_clarification"]
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load runs: {type(e).__name__}: {e}")
        return
    if not runs:
        st.info("No approved quote or clarification yet. Approve a draft in the Review workbench.")
        return
    ids = [r["rfq_id"] for r in runs]
    rid = common.rfq_picker(
        ids,
        "quote_pick",
        ids[0],
        format_func=lambda i: (
            f"{i} · " + next((r["quote_id"] or r["status"]) for r in runs if r["rfq_id"] == i)
        ),
    )
    view = svc.get(rid)
    if view is None:
        return

    if view.quote is None:
        if view.clarification:
            _clarification(view.clarification)
        return

    q = view.quote
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Quote", q.quote_id)
    c2.metric("Customer", q.customer_name)
    c3.metric("Valid until", q.valid_until.isoformat())
    c4.metric("Approved by", q.approved_by)
    if view.clarification:
        st.info("A clarification e-mail was also drafted for this RFQ.")

    html_path = Path(view.quote_paths["html"]) if view.quote_paths else None
    json_path = Path(view.quote_paths["json"]) if view.quote_paths else None
    d1, d2, _ = st.columns([1, 1, 4])
    if html_path and html_path.exists():
        d1.download_button(
            "Download HTML", html_path.read_bytes(), file_name=html_path.name, mime="text/html", key="dl_html"
        )
    if json_path and json_path.exists():
        d2.download_button(
            "Download JSON",
            json_path.read_bytes(),
            file_name=json_path.name,
            mime="application/json",
            key="dl_json",
        )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Quotation document")
        if html_path and html_path.exists():
            components.html(html_path.read_text(encoding="utf-8"), height=1400, scrolling=True)
        else:
            st.warning("Quote HTML file not found.")
    with right:
        st.subheader("Cover letter")
        st.caption(
            f"Text source: {q.cover_letter_source}. Prices in the letter are inserted from the calculated "
            "quote by a template, never written by the model."
        )
        with st.container(border=True):
            st.text(q.cover_letter)
        if q.review_comment:
            st.caption(f"Reviewer comment: {q.review_comment}")
        st.subheader("Assumptions")
        st.markdown("\n".join(f"- {a}" for a in q.assumptions))
