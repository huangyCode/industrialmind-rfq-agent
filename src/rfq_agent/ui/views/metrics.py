"""Metrics: evaluation reports, runtime statistics and the business view (README §7)."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import streamlit as st

from rfq_agent.config import ROOT
from rfq_agent.ui import common
from rfq_agent.ui.logic import (
    BASELINE_MINUTES,
    ENGINEER_EUR_PER_H,
    MINUTES_PER_TIER,
    business_view,
    trace_totals,
)

REPORT_DIR = ROOT / "eval" / "reports"
REPORT_ORDER = ["extraction", "backtest", "routing", "feedback"]
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
IMG_EXT = {".png", ".jpg", ".jpeg", ".svg", ".gif"}


def _render_markdown(path: Path) -> set[Path]:
    """Render a report; inline images that the markdown references are shown with st.image."""
    text = path.read_text(encoding="utf-8")
    shown: set[Path] = set()
    pos = 0
    for m in IMG_RE.finditer(text):
        if text[pos : m.start()].strip():
            st.markdown(text[pos : m.start()])
        img = (path.parent / m.group(2)).resolve()
        if img.exists():
            st.image(str(img), caption=m.group(1) or img.name)
            shown.add(img)
        else:
            st.caption(f"(image not found: {m.group(2)})")
        pos = m.end()
    if text[pos:].strip():
        st.markdown(text[pos:])
    return shown


def _reports() -> None:
    reports = sorted(REPORT_DIR.glob("*.md")) if REPORT_DIR.exists() else []
    if not reports:
        st.info("No evaluation reports yet. Run `make eval` to generate them in eval/reports/.")
        return
    reports.sort(key=lambda p: (REPORT_ORDER.index(p.stem) if p.stem in REPORT_ORDER else 99, p.stem))
    tabs = st.tabs([p.stem.replace("_", " ").capitalize() for p in reports] + ["Figures"])
    shown: set[Path] = set()
    for tab, p in zip(tabs, reports, strict=False):
        with tab:
            shown |= _render_markdown(p)
    with tabs[-1]:
        images = [p for p in sorted(REPORT_DIR.rglob("*")) if p.suffix.lower() in IMG_EXT]
        if not images:
            st.caption("No figures in eval/reports.")
        for img in images:
            st.image(str(img), caption=img.relative_to(REPORT_DIR).as_posix())


def _runtime(runs: list[dict]) -> None:
    svc = common.service()
    rows = []
    for r in runs:
        v = svc.get(r["rfq_id"])
        if v is None:
            continue
        t = trace_totals(v.trace)
        rows.append(
            {
                "RFQ": r["rfq_id"],
                "status": r["status"].replace("_", " "),
                "tier": ", ".join(r["tiers"].values()),
                "extractor": v.extractor,
                "steps": len(v.trace),
                "processing s": round(t["duration_s"], 1),
                "LLM calls": t["llm_calls"],
                "cache hits": t["cache_hits"],
                "tokens in": t["input_tokens"],
                "tokens out": t["output_tokens"],
                "edit rounds": v.review_round,
            }
        )
    if not rows:
        st.info("No runs yet.")
        return
    df = pd.DataFrame(rows)
    lines = [t for r in runs for t in r["tiers"].values()]
    c = st.columns(5)
    c[0].metric("Runs", len(df))
    c[1].metric("Avg processing time", f"{df['processing s'].mean():.1f} s")
    c[2].metric("LLM calls", int(df["LLM calls"].sum()))
    c[3].metric("Replayed from cache", int(df["cache hits"].sum()))
    c[4].metric("Quote lines triaged", len(lines))
    left, right = st.columns([3, 2])
    with left:
        st.dataframe(common.style_tier_column(df), hide_index=True, width="stretch")
    with right:
        dist = pd.DataFrame(
            {"lines": [lines.count(t) for t in MINUTES_PER_TIER]},
            index=[common.TIER_LABEL[t] for t in MINUTES_PER_TIER],
        )
        st.markdown("**Triage distribution (quote lines)**")
        st.bar_chart(dist, horizontal=True, height=200, color="#6b7280")
    st.caption(
        "Processing time = sum of the workflow step durations in the trace (model time included, engineer "
        "waiting time excluded). With the replay cache, model calls take milliseconds; live local-model "
        "runs take minutes per drawing."
    )


def _business(runs: list[dict]) -> None:
    b = business_view(runs)
    st.markdown(
        "Engineer time per RFQ, estimated from the triage tier. **Assumptions** (README §7, "
        "to be replaced by measured times in a pilot):"
    )
    st.dataframe(
        pd.DataFrame(
            [
                {"tier": common.TIER_LABEL[t], "engineer minutes per RFQ": m, "what the engineer does": d}
                for (t, m), d in zip(
                    MINUTES_PER_TIER.items(),
                    [
                        "check the draft and approve",
                        "review flagged items, adjust routing",
                        "full manual quotation (no time saved)",
                    ],
                    strict=True,
                )
            ]
            + [
                {
                    "tier": "Baseline (today)",
                    "engineer minutes per RFQ": BASELINE_MINUTES,
                    "what the engineer does": "manual quotation",
                }
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    if not b["rfqs"]:
        st.info("No triaged RFQs yet.")
        return
    c = st.columns(5)
    c[0].metric("Triaged RFQs", b["rfqs"])
    c[1].metric("Baseline (manual)", f"{b['baseline_minutes'] / 60:.1f} h")
    c[2].metric("With triage (est.)", f"{b['minutes'] / 60:.1f} h")
    c[3].metric("Time saved (est.)", f"{b['saved_minutes'] / 60:.1f} h ({b['saved_pct']:.0f}%)")
    c[4].metric(f"Value at EUR {ENGINEER_EUR_PER_H}/h", f"EUR {b['saved_eur']:,.0f}")
    st.caption(
        "RFQ tier = worst tier of its lines. "
        + (
            f"Not counted (no triage yet, e.g. waiting for clarification): {', '.join(b['not_counted'])}. "
            if b["not_counted"]
            else ""
        )
        + "Four demo RFQs are not a representative mix; the README scales the same assumptions to "
        "15,000 RFQs per year."
    )


def render() -> None:
    st.title("Metrics")
    st.caption("All data in this prototype is synthetic; figures show the method, not production results.")
    try:
        runs = common.service().list_runs()
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load runs: {type(e).__name__}: {e}")
        runs = []
    names = ["Evaluation reports", "Runtime", "Business view"]
    tab = st.query_params.get("tab")
    t1, t2, t3 = st.tabs(names, default=tab if tab in names else None)
    with t1:
        _reports()
    with t2:
        _runtime(runs)
    with t3:
        _business(runs)
