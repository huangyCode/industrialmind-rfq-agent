"""StateGraph assembly (DESIGN §4.1) and the SQLite checkpointer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from rfq_agent.config import CHECKPOINT_DB
from rfq_agent.graph.deps import Deps
from rfq_agent.graph.nodes import make_nodes, traced
from rfq_agent.graph.state import RFQState
from rfq_agent.models import RFQStatus

NODE_ORDER = (
    "intake",
    "extract_drawings",
    "validate",
    "clarify",
    "retrieve_similar",
    "plan_bom_routing",
    "cost",
    "assess",
    "human_review",
    "finalize",
)


def open_checkpointer(path: Path | str = CHECKPOINT_DB) -> SqliteSaver:
    """SQLite checkpointer; thread_id = rfq_id, so a pending review survives process restarts."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def _after_step(next_node: str):
    def route(state: RFQState) -> str:
        return END if state.get("status") == RFQStatus.ERROR.value else next_node

    return route


def _after_validate(state: RFQState) -> str:
    blocked = any(i["severity"] == "blocker" for i in state.get("issues", []))
    return "clarify" if blocked else "retrieve_similar"


def _after_review(state: RFQState) -> str:
    action = (state.get("review") or {}).get("action")
    return {
        "approve": "finalize",
        "edit": "cost",
        "reject": END,
        "request_clarification": "clarify",
    }.get(action, "human_review")  # invalid edit → ask again


def build_graph(deps: Deps, checkpointer=None):
    """Compile the RFQ workflow. `checkpointer=None` opens the SQLite store at CHECKPOINT_DB."""
    nodes = make_nodes(deps)
    g = StateGraph(RFQState)
    for name in NODE_ORDER:
        g.add_node(name, traced(name, nodes[name], deps))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", _after_step("extract_drawings"), ["extract_drawings", END])
    g.add_conditional_edges("extract_drawings", _after_step("validate"), ["validate", END])
    g.add_conditional_edges("validate", _after_validate, ["clarify", "retrieve_similar"])
    g.add_edge("clarify", END)
    g.add_edge("retrieve_similar", "plan_bom_routing")
    g.add_edge("plan_bom_routing", "cost")
    g.add_edge("cost", "assess")
    g.add_edge("assess", "human_review")
    g.add_conditional_edges(
        "human_review", _after_review, ["finalize", "cost", "clarify", "human_review", END]
    )
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else open_checkpointer())


def mermaid(deps: Deps) -> str:
    """Mermaid source of the workflow (for docs / UI)."""
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(deps, checkpointer=InMemorySaver()).get_graph().draw_mermaid()
