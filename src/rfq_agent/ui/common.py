"""Shared helpers for the Streamlit workbench: the cached service, navigation and small display widgets.

The UI talks to the pipeline only through `rfq_agent.app_api` (RFQService / RunView), never to graph
internals. Tests inject an isolated service with `app_api.set_service(...)` and clear the resource cache.
"""

from __future__ import annotations

import threading
from typing import Any

import streamlit as st

from rfq_agent import app_api

TIER_COLOR = {"fast_track": "green", "standard": "orange", "manual": "red"}
TIER_LABEL = {"fast_track": "Fast track", "standard": "Standard review", "manual": "Manual"}
TIER_BG = {"fast_track": "#d9f2e0", "standard": "#fdebc8", "manual": "#f9d5d3"}
SEV_COLOR = {"blocker": "red", "warning": "orange", "info": "blue"}
STATUS_COLOR = {
    "approved": "green",
    "in_review": "orange",
    "needs_clarification": "violet",
    "rejected": "gray",
    "error": "red",
}
EXTRACTORS = {
    "llm": "LLM (recorded / replay)",
    "reference": "Reference data (gold, no model)",
}

# StreamlitPage objects, registered by app.py on every run (used by goto()).
PAGES: dict[str, Any] = {}


class _Locked:
    """Serialises graph runs across browser sessions (one checkpoint DB, one SQLite connection)."""

    def __init__(self, svc: app_api.RFQService):
        self.svc = svc
        self.lock = threading.Lock()


@st.cache_resource(show_spinner="Loading master data, knowledge base and LLM client ...")
def _service() -> _Locked:
    return _Locked(app_api.service())


def service() -> app_api.RFQService:
    return _service().svc


def run_lock() -> threading.Lock:
    return _service().lock


def reset_service_cache() -> None:
    """For tests: forget the cached service so the next run picks up app_api.set_service()."""
    _service.clear()


# ---------- navigation ----------


def current_rfq() -> str | None:
    rid = st.session_state.get("rfq_id")
    if rid:
        return rid
    rid = st.query_params.get("rfq")
    if rid:
        st.session_state["rfq_id"] = rid
    return rid


def goto(page: str, rfq_id: str | None = None) -> None:
    if rfq_id:
        st.session_state["rfq_id"] = rfq_id
    target = PAGES.get(page)
    if target is not None:
        st.switch_page(target)


# ---------- widgets ----------


def tier_badge(tier: str | None, prefix: str = "") -> None:
    if not tier:
        return
    st.badge(prefix + TIER_LABEL.get(tier, tier), color=TIER_COLOR.get(tier, "gray"))


def status_badge(status: str) -> None:
    st.badge(status.replace("_", " "), color=STATUS_COLOR.get(status, "blue"))


def worst_tier(tiers: dict) -> str | None:
    order = ["manual", "standard", "fast_track"]
    vals = [str(getattr(t, "value", t)) for t in tiers.values()]
    return next((t for t in order if t in vals), None)


def eur(x: float | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:,.{digits}f} EUR"


def style_tier_column(df, column: str = "tier"):
    """pandas Styler colouring a tier column with the workbench tier colours."""

    def colour(v):
        bg = TIER_BG.get(str(v).split(",")[0].split(":")[-1].strip()) if v else None
        return f"background-color: {bg}" if bg else ""

    return df.style.map(colour, subset=[column])


def inject_css() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 2.2rem; max-width: 1500px;}
        div[data-testid="stMetricValue"] {font-size: 1.35rem;}
        .rfq-muted {color: #6b7280; font-size: 0.85rem;}
        .rfq-evidence {font-family: ui-monospace, monospace; font-size: 0.8rem; color:#374151;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def rfq_picker(ids: list[str], key: str, default: str, **kwargs) -> str:
    """Selectbox over run ids that stays in sync with the RFQ selected on other pages."""
    rid = current_rfq()
    if rid not in ids:
        rid = default
    if st.session_state.get(key) != rid:
        st.session_state[key] = rid

    def _changed() -> None:
        st.session_state["rfq_id"] = st.session_state[key]

    pick = st.selectbox("RFQ", ids, key=key, on_change=_changed, **kwargs)
    st.session_state["rfq_id"] = pick
    return pick
