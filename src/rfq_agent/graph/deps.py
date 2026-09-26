"""Dependencies injected into the graph: LLM client, repositories, knowledge base and drawing extractors.

Product code never switches on a "gold mode": the extractor is an injected object. The default is the
model-based extractor; `ReferenceExtractor` reads the hand-written reference/gold data under
`data/samples/**/expected/` and exists for testing and demos without a model.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from rfq_agent.agents.knowledge import KnowledgeBase
from rfq_agent.config import QUOTE_DIR
from rfq_agent.data.repositories import (
    CatalogRepo,
    FeedbackRepo,
    MaterialRepo,
    PartRepo,
    QuoteRepo,
    RateRepo,
    ServiceRepo,
)
from rfq_agent.models import DrawingSpec, RFQRequest


class Extractor(Protocol):
    """Reads an RFQ folder and its drawings into structured data."""

    name: str

    def parse_rfq(self, rfq_dir: Path) -> RFQRequest: ...

    def extract(self, pdf: Path, rfq_id: str | None = None) -> tuple[DrawingSpec, list[Path]]: ...


class LLMExtractor:
    """Default extractor: intake + drawing agents backed by the LLM client (record/replay aware)."""

    name = "llm"

    def __init__(self, client: Any, materials: MaterialRepo):
        self.client = client
        self.materials = materials

    def parse_rfq(self, rfq_dir: Path) -> RFQRequest:
        from rfq_agent.agents.intake import parse_rfq

        return parse_rfq(Path(rfq_dir), self.client)

    def extract(self, pdf: Path, rfq_id: str | None = None) -> tuple[DrawingSpec, list[Path]]:
        from rfq_agent.agents.drawing import extract_drawing

        return extract_drawing(Path(pdf), self.client, self.materials, rfq_id=rfq_id)


class ReferenceExtractor:
    """Reference / gold data, for testing without a model.

    Reads `<rfq_dir>/expected/rfq.json` and `<rfq_dir>/expected/<drawing stem>.json` (hand-written ground
    truth of the synthetic samples). Never used unless explicitly selected.
    """

    name = "reference"
    client = None

    def parse_rfq(self, rfq_dir: Path) -> RFQRequest:
        rfq_dir = Path(rfq_dir)
        p = rfq_dir / "expected" / "rfq.json"
        if not p.exists():
            raise FileNotFoundError(f"No reference data {p} (reference extractor only works on samples)")
        req = RFQRequest.model_validate_json(p.read_text(encoding="utf-8"))
        req.rfq_id = rfq_dir.name
        return req

    def extract(self, pdf: Path, rfq_id: str | None = None) -> tuple[DrawingSpec, list[Path]]:
        pdf = Path(pdf)
        p = pdf.parent.parent / "expected" / f"{pdf.stem}.json"
        if not p.exists():
            raise FileNotFoundError(f"No reference drawing data {p}")
        spec = DrawingSpec.model_validate_json(p.read_text(encoding="utf-8"))
        preview = pdf.with_suffix(".png")
        return spec, [preview] if preview.exists() else []


EXTRACTOR_NAMES = ("llm", "reference")


@dataclass
class Deps:
    """Everything the nodes need. Build with `Deps.default()` or construct directly in tests."""

    client: Any  # LLMClient-like (text/structured/stats) for prose; None = template text only
    materials: MaterialRepo
    parts: PartRepo
    rates: RateRepo
    services: dict
    catalog: CatalogRepo
    quotes: QuoteRepo
    feedback: FeedbackRepo
    kb: KnowledgeBase
    quote_dir: Path = QUOTE_DIR
    today: date | None = None  # fixed "today" for reproducible lead-time checks; None = date.today()
    extractors: dict[str, Any] = field(default_factory=dict)
    extractor_factories: dict[str, Callable[[], Any]] = field(default_factory=dict)

    @classmethod
    def from_conn(
        cls,
        conn: sqlite3.Connection,
        *,
        client: Any = None,
        extractors: dict[str, Any] | None = None,
        quote_dir: Path = QUOTE_DIR,
        today: date | None = None,
        kb: KnowledgeBase | None = None,
    ) -> Deps:
        mats = MaterialRepo(conn)
        deps = cls(
            client=client,
            materials=mats,
            parts=PartRepo(conn),
            rates=RateRepo(conn),
            services=ServiceRepo(conn).all,
            catalog=CatalogRepo(conn),
            quotes=QuoteRepo(conn),
            feedback=FeedbackRepo(conn),
            kb=kb or KnowledgeBase.load(),
            quote_dir=Path(quote_dir),
            today=today,
            extractors=dict(extractors or {}),
        )
        deps.extractor_factories.setdefault("reference", ReferenceExtractor)
        if client is not None:
            deps.extractor_factories.setdefault("llm", lambda: LLMExtractor(deps.client, deps.materials))
        return deps

    @classmethod
    def default(cls, *, quote_dir: Path = QUOTE_DIR) -> Deps:
        """Production wiring: SQLite repos at DB_PATH and the LLM client from the environment."""
        from rfq_agent.data.db import connect
        from rfq_agent.llm.client import get_client

        return cls.from_conn(connect(), client=get_client(), quote_dir=quote_dir)

    def today_or_now(self) -> date:
        return self.today or date.today()

    def extractor(self, name: str) -> Any:
        if name not in self.extractors:
            if name not in self.extractor_factories:
                raise ValueError(
                    f"Unknown extractor '{name}'; choose one of {sorted(self.extractor_factories)}"
                )
            self.extractors[name] = self.extractor_factories[name]()
        return self.extractors[name]

    def clients(self) -> list[Any]:
        """Distinct LLM clients whose stats feed the trace."""
        out: list[Any] = []
        for c in [self.client, *(getattr(e, "client", None) for e in self.extractors.values())]:
            if c is not None and hasattr(c, "stats") and all(c is not o for o in out):
                out.append(c)
        return out
