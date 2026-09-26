"""Knowledge base search: the engineering rules that DFM findings and quoting policy cite."""

from __future__ import annotations

import streamlit as st

from rfq_agent.ui import common

MIN_SCORE = 0.08  # below this, retrieval results are shown as "not a match"


def render() -> None:
    st.title("Knowledge base")
    st.caption(
        "Engineering guidelines, plant capabilities, material standards and quoting policy. Findings in the "
        "Review workbench link to these sections by id. Retrieval only; answers are not generated."
    )
    c1, c2 = st.columns([5, 1])
    query = c1.text_input("Search", value="grinding k6 bearing seat", key="kb_query")
    k = c2.number_input("Results", min_value=1, max_value=10, value=4, key="kb_k")
    if not query.strip():
        return
    try:
        hits = common.service().kb_search(query, int(k))
    except Exception as e:  # noqa: BLE001
        st.error(f"Search failed: {type(e).__name__}: {e}")
        return
    if not hits:
        st.info("No matching section.")
        return
    if hits[0]["score"] < MIN_SCORE:
        st.warning(
            f"No section matches this query well (best score {hits[0]['score']:.3f} < {MIN_SCORE}). "
            "The knowledge base is written in English: try English terms, e.g. 'deep hole drilling'. "
            "Closest sections below, for reference only."
        )
    for h in hits:
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.markdown(f"**{h['heading']}**  \n`{h['id']}`")
            b.metric("score", f"{h['score']:.3f}")
            st.markdown(h["text"])
