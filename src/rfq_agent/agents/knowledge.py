"""Engineering knowledge base: markdown docs chunked by heading, TF-IDF retrieval (no LLM)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from rfq_agent.config import KNOWLEDGE_DIR

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")  # H1 only ends a chunk; ## / ### start one


def slugify(heading: str) -> str:
    """'Heat treatment sequence' -> 'heat-treatment-sequence' (ASCII, lowercase, hyphen-separated)."""
    s = unicodedata.normalize("NFKD", heading).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return s.strip("-") or "section"


@dataclass
class Chunk:
    id: str
    file: str
    heading: str
    text: str


def chunk_markdown(file: str, content: str) -> list[Chunk]:
    """Split one document at ## / ### headings; the H1 title and its preamble are not indexed."""
    chunks: list[Chunk] = []
    seen: set[str] = set()
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        if heading is None or not text:
            return
        slug = base = slugify(heading)
        n = 2
        while slug in seen:
            slug, n = f"{base}-{n}", n + 1
        seen.add(slug)
        chunks.append(Chunk(id=f"{file}#{slug}", file=file, heading=heading, text=text))

    in_code = False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m:
            flush()
            heading, buf = (m.group(2) if len(m.group(1)) > 1 else None), []
        else:
            buf.append(line)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._by_id = {c.id: c for c in chunks}
        docs = [f"{c.heading} {c.heading} {c.text}" for c in chunks]
        self._word = TfidfVectorizer(lowercase=True, sublinear_tf=True, ngram_range=(1, 2))
        self._char = TfidfVectorizer(
            lowercase=True, sublinear_tf=True, analyzer="char_wb", ngram_range=(3, 5)
        )
        self._wm = self._word.fit_transform(docs) if docs else None
        self._cm = self._char.fit_transform(docs) if docs else None

    @classmethod
    def load(cls, dir: Path = KNOWLEDGE_DIR) -> KnowledgeBase:
        chunks: list[Chunk] = []
        for path in sorted(Path(dir).glob("*.md")):
            chunks.extend(chunk_markdown(path.name, path.read_text(encoding="utf-8")))
        return cls(chunks)

    def get(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def ids(self) -> list[str]:
        return [c.id for c in self.chunks]

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        """Cosine similarity, mean of word (1-2 gram) and char (3-5 gram) TF-IDF spaces."""
        if not self.chunks or not query.strip():
            return []
        sw = linear_kernel(self._word.transform([query]), self._wm).ravel()
        sc = linear_kernel(self._char.transform([query]), self._cm).ravel()
        scores = 0.5 * sw + 0.5 * sc
        order = np.argsort(-scores, kind="stable")[:k]
        return [(self.chunks[i], float(scores[i])) for i in order]
