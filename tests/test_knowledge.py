from __future__ import annotations

import re

import pytest

from rfq_agent.agents.knowledge import KnowledgeBase, chunk_markdown, slugify
from rfq_agent.agents.review_rules import RULES
from rfq_agent.config import KNOWLEDGE_DIR

EXPECTED_DOCS = {
    "dfm_guidelines.md",
    "tolerance_guide.md",
    "material_standards.md",
    "quoting_policy.md",
    "plant_capabilities.md",
}


@pytest.fixture(scope="module")
def kb() -> KnowledgeBase:
    return KnowledgeBase.load()


def test_slugify():
    assert slugify("Deep holes") == "deep-holes"
    assert slugify("IT grades") == "it-grades"
    assert slugify("RFQ completeness") == "rfq-completeness"
    assert slugify("Plant DE (Augsburg)") == "plant-de-augsburg"
    assert slugify("Größe & Güte") == "groe-gute"


def test_chunking_and_duplicate_slugs():
    md = "# Title\nintro\n## A b\ntext a\n### C\ntext c\n## A b\nagain\n```\n## not a heading\n```\n"
    chunks = chunk_markdown("x.md", md)
    assert [c.id for c in chunks] == ["x.md#a-b", "x.md#c", "x.md#a-b-2"]
    assert "## not a heading" in chunks[-1].text


def test_docs_present_and_sized():
    names = {p.name for p in KNOWLEDGE_DIR.glob("*.md")}
    assert EXPECTED_DOCS <= names
    for n in EXPECTED_DOCS:
        lines = (KNOWLEDGE_DIR / n).read_text(encoding="utf-8").splitlines()
        assert 60 <= len(lines) <= 150, (n, len(lines))


def test_chunk_ids_unique(kb):
    ids = kb.ids()
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z_]+\.md#[a-z0-9-]+", i) for i in ids)


def test_every_rule_kb_ref_resolves(kb):
    for r in RULES:
        chunk = kb.get(r.kb_ref)
        assert chunk is not None, f"{r.id}: {r.kb_ref}"
        assert chunk.text.strip()


def test_reuse_section_exists(kb):
    c = kb.get("quoting_policy.md#part-reuse")
    assert c is not None and "0.92" in c.text


def test_plant_capabilities_consistent_with_master(kb, db):
    """Equipment matrix in the KB must match work_centers.csv."""
    have = {(r[0], r[1]) for r in db.execute("SELECT plant, code FROM work_centers")}
    text = kb.get("plant_capabilities.md#equipment-matrix").text
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "`" not in ln]
    checked = 0
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) != 5 or cells[2] not in ("yes", "no"):
            continue
        code = cells[1]
        for plant, val in zip(("DE", "PL", "CN"), cells[2:], strict=True):
            assert ((plant, code) in have) == (val == "yes"), (plant, code)
        checked += 1
    assert checked == len({c for _, c in have})


@pytest.mark.parametrize(
    "query, expected",
    [
        ("deep hole drilling", "dfm_guidelines.md#deep-holes"),
        ("gun drilling depth to diameter ratio", "dfm_guidelines.md#deep-holes"),
        ("minimum order value", "quoting_policy.md#minimum-order-value"),
        ("AISI 4140 equivalent", "material_standards.md#equivalents"),
        ("ISO 2768 general tolerance", "tolerance_guide.md#general-tolerances"),
    ],
)
def test_search_top1(kb, query, expected):
    hits = kb.search(query)
    assert hits[0][0].id == expected, [(c.id, round(s, 3)) for c, s in hits]


def test_search_basics(kb):
    hits = kb.search("which plant has no cylindrical grinding", k=3)
    assert len(hits) == 3
    assert hits[0][1] >= hits[1][1] >= hits[2][1] > 0
    assert hits[0][0].file == "plant_capabilities.md"
    assert kb.search("   ") == []
    assert kb.get("nope.md#x") is None
