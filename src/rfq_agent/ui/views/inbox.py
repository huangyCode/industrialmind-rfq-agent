"""Inbox: sample RFQs to run, plus every run in the checkpoint store."""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from rfq_agent import app_api
from rfq_agent.ui import common
from rfq_agent.ui.logic import trace_frame, trace_totals


def _run(sample: dict, extractor: str, auto_approve: bool) -> None:
    svc = common.service()
    t0 = time.perf_counter()
    try:
        with (
            common.run_lock(),
            st.spinner(f"Running {sample['rfq_id']} (intake, drawings, validation, costing) ..."),
        ):
            view = svc.start(sample["path"], extractor, auto_approve=auto_approve)
    except Exception as e:  # noqa: BLE001 - shown to the user, the app keeps running
        st.session_state["last_run"] = {"rfq_id": sample["rfq_id"], "error": f"{type(e).__name__}: {e}"}
        return
    st.session_state["rfq_id"] = view.rfq_id
    st.session_state["last_run"] = {
        "rfq_id": view.rfq_id,
        "wall_s": time.perf_counter() - t0,
        "auto_approved": view.auto_approved,
    }


def _last_run() -> None:
    info = st.session_state.get("last_run")
    if not info:
        return
    if info.get("error"):
        st.error(f"Run of {info['rfq_id']} failed: {info['error']}")
        return
    view = common.service().get(info["rfq_id"])
    if view is None:
        return
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"**Last run: {view.rfq_id}**")
            b = st.columns(4, gap="small")
            with b[0]:
                common.status_badge(view.status)
            for i, (ln, tier) in enumerate(sorted(view.tiers.items())):
                with b[min(1 + i, 3)]:
                    common.tier_badge(tier.value, prefix=f"Line {ln}: ")
            tot = trace_totals(view.trace)
            st.caption(
                f"{len(view.trace)} workflow steps · {info['wall_s']:.1f} s wall time · "
                f"{tot['llm_calls']} LLM calls · {tot['cache_hits']} replayed from cache · "
                f"extractor: {common.EXTRACTORS.get(view.extractor or '', view.extractor)}"
                + (" · auto-approved (all lines fast track)" if info.get("auto_approved") else "")
            )
            if view.error:
                st.error(view.error)
            for raw in view.lines:
                if raw.get("extraction_error"):
                    st.error(f"Line {raw['line_no']}: drawing extraction failed: {raw['extraction_error']}")
        with c2:
            if st.button("Open in Review", key="open_last", type="primary", width="stretch"):
                common.goto("review", view.rfq_id)
            if view.quote or view.clarification:
                label = "Open quote" if view.quote else "Open clarification e-mail"
                if st.button(label, key="open_last_quote", width="stretch"):
                    common.goto("quote", view.rfq_id)
        with st.expander("Workflow trace (steps, timings, LLM calls)", expanded=True):
            st.dataframe(trace_frame(view.trace), hide_index=True, width="stretch")


def render() -> None:
    st.title("Inbox")
    st.caption(
        "Incoming requests for quotation. Running an RFQ executes the LangGraph workflow up to the engineer "
        "review (or the clarification e-mail when a blocker is found)."
    )
    try:
        svc = common.service()
        samples = app_api.list_samples()
        runs = {r["rfq_id"]: r for r in svc.list_runs()}
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load the service: {type(e).__name__}: {e}")
        return

    opt1, opt2 = st.columns([2, 1])
    with opt1:
        extractor = st.radio(
            "Extractor",
            list(common.EXTRACTORS),
            format_func=common.EXTRACTORS.get,
            horizontal=True,
            key="extractor",
            help="LLM: the model reads e-mail and drawing (recorded model output is replayed when "
            "available). Reference: hand-written ground truth of the synthetic samples, no model.",
        )
    with opt2:
        auto = st.checkbox(
            "Auto-approve fast track",
            value=True,
            key="auto_approve",
            help="Approve automatically only if every line is FAST_TRACK. Other tiers always wait "
            "for an engineer.",
        )

    st.subheader("New requests")
    head = st.columns([1.3, 3.2, 1.3, 1.6, 1.2, 0.9])
    for col, label in zip(head, ["RFQ", "Subject", "Drawings", "Last run", "Tier", ""], strict=True):
        col.markdown(f"<span class='rfq-muted'>{label}</span>", unsafe_allow_html=True)
    for s in samples:
        r = runs.get(s["rfq_id"])
        cols = st.columns([1.3, 3.2, 1.3, 1.6, 1.2, 0.9], vertical_alignment="center")
        cols[0].markdown(f"**{s['rfq_id']}**")
        cols[1].write(s["subject"])
        cols[2].write(", ".join(d.removesuffix(".pdf") for d in s["drawings"]))
        with cols[3]:
            if r:
                common.status_badge(r["status"])
            else:
                st.caption("not run")
        with cols[4]:
            if r and r["tiers"]:
                common.tier_badge(common.worst_tier(r["tiers"]))
        label, hint = (
            ("Re-run", "Starts a fresh run; the previous run of this RFQ is replaced.")
            if r
            else ("Run", None)
        )
        if cols[5].button(label, key=f"run_{s['rfq_id']}", width="stretch", help=hint):
            _run(s, extractor, auto)
            st.rerun()

    _last_run()

    st.subheader("All runs")
    if not runs:
        st.info("No runs yet. Run one of the requests above.")
        return
    df = pd.DataFrame(
        [
            {
                "RFQ": r["rfq_id"],
                "customer": r["customer"],
                "status": r["status"].replace("_", " "),
                "waiting for review": "yes" if r["pending_review"] else "",
                "tier": ", ".join(f"L{k}: {v}" for k, v in r["tiers"].items()),
                "quote": r["quote_id"] or "",
                "updated": (r["updated_at"] or "")[:19].replace("T", " "),
            }
            for r in runs.values()
        ]
    )
    st.dataframe(common.style_tier_column(df), hide_index=True, width="stretch")
    c1, c2, _ = st.columns([2, 1, 3], vertical_alignment="bottom")
    pick = c1.selectbox("Open run", list(runs), key="inbox_pick")
    if c2.button("Open in Review", key="open_pick", width="stretch"):
        common.goto("review", pick)
