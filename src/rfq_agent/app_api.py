"""Synchronous facade over the RFQ graph for the CLI and the Streamlit UI.

Module-level functions use one lazily created default service (real DB, LLM client from the environment,
checkpoints at CHECKPOINT_DB). Tests / the UI can build their own `RFQService(deps, checkpoint_db)`.

    list_samples() -> list[dict]
    start(rfq_dir, extractor="llm", auto_approve=False) -> RunView
    get(rfq_id) -> RunView | None
    review(rfq_id, ReviewDecision) -> RunView
    list_runs() -> list[dict]
    kb_search(query, k=4) -> list[dict]
    similar(spec_or_path, qty=200) -> list[SimilarPart]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.types import Command

from rfq_agent.agents.postprocess import postprocess
from rfq_agent.agents.similarity import find_similar
from rfq_agent.config import CHECKPOINT_DB, SAMPLES_DIR
from rfq_agent.graph.build import build_graph, open_checkpointer
from rfq_agent.graph.deps import Deps, LLMExtractor
from rfq_agent.graph.edits import EditError, apply_edits
from rfq_agent.models import (
    DrawingSpec,
    FeedbackRecord,
    Quote,
    QuoteDraft,
    ReviewDecision,
    RFQRequest,
    SimilarPart,
    Tier,
    TraceEvent,
    ValidationIssue,
)

AUTO_REVIEWER = "auto-approve (fast track)"


@dataclass
class RunView:
    """Everything the UI needs about one RFQ run (typed models, not raw state)."""

    rfq_id: str
    status: str  # RFQStatus value
    pending_review: bool  # waiting at human_review
    next_nodes: list[str]
    extractor: str | None
    request: RFQRequest | None
    issues: list[ValidationIssue]
    draft: QuoteDraft | None  # present once assess ran
    quote: Quote | None
    quote_paths: dict | None
    clarification: dict | None  # {"to", "subject", "body", "questions", "language", "source"}
    trace: list[TraceEvent]
    reviews: list[dict]  # decision history
    review_round: int
    review_error: str | None
    error: str | None
    lines: list[dict] = field(default_factory=list)  # raw per-line work products (JSON)
    auto_approved: bool = False

    @property
    def tiers(self) -> dict[int, Tier]:
        return {ln.line_no: ln.confidence.tier for ln in self.draft.lines} if self.draft else {}


class RFQService:
    def __init__(self, deps: Deps, checkpoint_db: Path | str = CHECKPOINT_DB):
        self.deps = deps
        self.saver = open_checkpointer(checkpoint_db)
        self.graph = build_graph(deps, self.saver)

    @staticmethod
    def _cfg(rfq_id: str) -> dict:
        return {"configurable": {"thread_id": rfq_id}}

    # ---------- runs ----------

    def start(self, rfq_dir: Path | str, extractor: str = "llm", *, auto_approve: bool = False) -> RunView:
        """Run a fresh RFQ until the review interrupt or the end. Re-running an RFQ starts over."""
        rfq_dir = Path(rfq_dir).resolve()
        if not (rfq_dir / "email.txt").exists() and extractor != "reference":
            raise FileNotFoundError(f"{rfq_dir} has no email.txt")
        self.deps.extractor(extractor)  # fail fast on an unknown extractor
        rfq_id = rfq_dir.name
        self.saver.delete_thread(rfq_id)
        self.graph.invoke(
            {
                "rfq_id": rfq_id,
                "rfq_dir": str(rfq_dir),
                "extractor": extractor,
                "review_round": 0,
                "plant_override": [],
            },
            self._cfg(rfq_id),
        )
        view = self.get(rfq_id)
        if (
            auto_approve
            and view.pending_review
            and view.tiers
            and all(t == Tier.FAST_TRACK for t in view.tiers.values())
        ):
            view = self.review(rfq_id, ReviewDecision(action="approve", reviewer=AUTO_REVIEWER))
            view.auto_approved = True
        return view

    def review(self, rfq_id: str, decision: ReviewDecision | dict) -> RunView:
        """Resume a pending review. Edits are validated first (EditError / ValueError on bad input)."""
        decision = ReviewDecision.model_validate(decision)
        snap = self.graph.get_state(self._cfg(rfq_id))
        if "human_review" not in (snap.next or ()):
            raise ValueError(f"{rfq_id} is not waiting for review (next: {list(snap.next or ())})")
        if decision.action == "edit":
            if not decision.edits:
                raise EditError("edit decision without edits")
            v = snap.values
            apply_edits(v["lines"], v.get("margin_pct", 0.0), v.get("plant_override", []), decision.edits)
        self.graph.invoke(Command(resume=decision.model_dump(mode="json")), self._cfg(rfq_id))
        return self.get(rfq_id)

    def get(self, rfq_id: str) -> RunView | None:
        snap = self.graph.get_state(self._cfg(rfq_id))
        v = snap.values or {}
        if not v:
            return None
        nxt = list(snap.next or ())

        def opt(model, key):
            return model.model_validate(v[key]) if v.get(key) else None

        return RunView(
            rfq_id=rfq_id,
            status=v.get("status", "new"),
            pending_review="human_review" in nxt,
            next_nodes=nxt,
            extractor=v.get("extractor"),
            request=opt(RFQRequest, "request"),
            issues=[ValidationIssue.model_validate(i) for i in v.get("issues", [])],
            draft=opt(QuoteDraft, "draft"),
            quote=opt(Quote, "quote"),
            quote_paths=v.get("quote_paths"),
            clarification=v.get("clarification"),
            trace=[TraceEvent.model_validate(t) for t in v.get("trace", [])],
            reviews=list(v.get("reviews", [])),
            review_round=v.get("review_round", 0),
            review_error=v.get("review_error"),
            error=v.get("error"),
            lines=list(v.get("lines", [])),
        )

    def list_runs(self) -> list[dict]:
        ids = [
            r[0]
            for r in self.saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")
        ]
        out = []
        for rid in ids:
            view = self.get(rid)
            if view is None:
                continue
            out.append(
                {
                    "rfq_id": rid,
                    "status": view.status,
                    "pending_review": view.pending_review,
                    "customer": view.request.customer_name if view.request else None,
                    "tiers": {k: t.value for k, t in view.tiers.items()},
                    "quote_id": view.quote.quote_id if view.quote else None,
                    "updated_at": view.trace[-1].started_at.isoformat() if view.trace else None,
                }
            )
        return out

    def feedback(self, rfq_id: str | None = None) -> list[FeedbackRecord]:
        import json

        out = []
        for r in self.deps.feedback.list():
            if rfq_id and r["rfq_id"] != rfq_id:
                continue
            after = json.loads(r["after"])
            out.append(
                FeedbackRecord(
                    rfq_id=r["rfq_id"],
                    line_no=r["line_no"],
                    reviewer=r["reviewer"],
                    edits=json.loads(r["edits"]),
                    before=json.loads(r["before"]),
                    after=after,
                    created_at=r["created_at"],
                    diff=after.get("diff", []),
                )
            )
        return out

    # ---------- lookups ----------

    def kb_search(self, query: str, k: int = 4) -> list[dict]:
        return [
            {"id": c.id, "file": c.file, "heading": c.heading, "text": c.text, "score": round(s, 3)}
            for c, s in self.deps.kb.search(query, k=k)
        ]

    def kb_get(self, chunk_id: str) -> dict | None:
        c = self.deps.kb.get(chunk_id)
        return {"id": c.id, "file": c.file, "heading": c.heading, "text": c.text} if c else None

    def load_spec(self, path: Path | str) -> DrawingSpec:
        """DrawingSpec from a JSON file, or extracted from a PDF with the LLM extractor."""
        path = Path(path)
        if path.suffix.lower() == ".json":
            spec = DrawingSpec.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            ex = (
                self.deps.extractor("llm")
                if "llm" in self.deps.extractor_factories
                else LLMExtractor(self.deps.client, self.deps.materials)
            )
            spec, _ = ex.extract(path)
        return postprocess(spec, self.deps.materials)

    def similar(self, spec: DrawingSpec | Path | str, qty: int = 200) -> list[SimilarPart]:
        if not isinstance(spec, DrawingSpec):
            spec = self.load_spec(spec)
        return find_similar(spec, self.deps.parts, self.deps.materials, qty=qty)


def list_samples(root: Path = SAMPLES_DIR) -> list[dict]:
    out = []
    for d in sorted(Path(root).glob("RFQ-*")):
        if not (d / "email.txt").exists():
            continue
        subject = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in (d / "email.txt").read_text(encoding="utf-8").splitlines()
                if ln.lower().startswith("subject:")
            ),
            "",
        )
        out.append(
            {
                "rfq_id": d.name,
                "path": str(d),
                "subject": subject,
                "drawings": sorted(p.name for p in (d / "drawings").glob("*.pdf")),
                "has_reference": (d / "expected" / "rfq.json").exists(),
            }
        )
    return out


# ---------- module-level default service ----------

_service: RFQService | None = None


def service() -> RFQService:
    global _service
    if _service is None:
        _service = RFQService(Deps.default())
    return _service


def set_service(svc: RFQService | None) -> None:
    global _service
    _service = svc


def start(rfq_dir: Path | str, extractor: str = "llm", *, auto_approve: bool = False) -> RunView:
    return service().start(rfq_dir, extractor, auto_approve=auto_approve)


def get(rfq_id: str) -> RunView | None:
    return service().get(rfq_id)


def review(rfq_id: str, decision: ReviewDecision | dict[str, Any]) -> RunView:
    return service().review(rfq_id, decision)


def list_runs() -> list[dict]:
    return service().list_runs()


def kb_search(query: str, k: int = 4) -> list[dict]:
    return service().kb_search(query, k)


def similar(spec: DrawingSpec | Path | str, qty: int = 200) -> list[SimilarPart]:
    return service().similar(spec, qty)


def feedback(rfq_id: str | None = None) -> list[FeedbackRecord]:
    return service().feedback(rfq_id)
