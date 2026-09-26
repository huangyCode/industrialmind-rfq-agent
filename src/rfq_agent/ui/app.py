"""Engineer workbench (Streamlit). Run with `make ui` or `uv run streamlit run src/rfq_agent/ui/app.py`."""

from __future__ import annotations

import streamlit as st

from rfq_agent.ui import common
from rfq_agent.ui.views import inbox, knowledge, metrics, quote, review


def main() -> None:
    st.set_page_config(page_title="RFQ & Quotation Agent", layout="wide")
    common.inject_css()
    pages = {
        "inbox": st.Page(inbox.render, title="Inbox", url_path="inbox", default=True),
        "review": st.Page(review.render, title="Review workbench", url_path="review"),
        "quote": st.Page(quote.render, title="Quote", url_path="quote"),
        "knowledge": st.Page(knowledge.render, title="Knowledge base", url_path="knowledge"),
        "metrics": st.Page(metrics.render, title="Metrics", url_path="metrics"),
    }
    common.PAGES.clear()
    common.PAGES.update(pages)
    nav = st.navigation(
        {
            "Quotation": [pages["inbox"], pages["review"], pages["quote"]],
            "Reference": [pages["knowledge"], pages["metrics"]],
        }
    )
    with st.sidebar:
        st.markdown("**PrecisionMotion GmbH**  \nAI RFQ & Quotation Agent")
        st.caption(
            "Prototype. The model reads e-mails and drawings and writes prose; routing, costs and prices "
            "are computed by deterministic code from master data. Fictional company, synthetic data."
        )
        rid = common.current_rfq()
        if rid:
            st.caption(f"Selected RFQ: **{rid}**")
    nav.run()


main()
