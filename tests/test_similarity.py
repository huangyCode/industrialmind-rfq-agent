"""Similar-part retrieval (DESIGN §5.5, CONTRACTS §1.8)."""

from __future__ import annotations

import pytest
from gen_history import load_specs

from rfq_agent.agents.knowledge import KnowledgeBase
from rfq_agent.agents.postprocess import postprocess
from rfq_agent.agents.similarity import (
    WEIGHTS,
    featurize_part,
    featurize_spec,
    find_similar,
    reuse_hint,
    score_features,
)
from rfq_agent.config import BIZ, SAMPLES_DIR
from rfq_agent.data.repositories import MaterialRepo, PartRepo
from rfq_agent.models import DrawingSpec, Severity, ShapeClass


@pytest.fixture(scope="module")
def repos(db):
    return MaterialRepo(db), PartRepo(db)


@pytest.fixture(scope="module")
def history(db):
    return load_specs()


def gold(rfq: str, pn: str, mats: MaterialRepo) -> DrawingSpec:
    p = SAMPLES_DIR / rfq / "expected" / f"{pn}.json"
    return postprocess(DrawingSpec.model_validate_json(p.read_text(encoding="utf-8")), mats)


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_featurize_spec_equals_featurize_part(repos, history):
    mats, parts = repos
    assert len(history) == len(parts.all()) == 90
    for rec in history:
        row = parts.get(rec["part_id"])
        assert row["part_number"] == rec["part_number"]
        assert featurize_spec(rec["spec"], mats) == featurize_part(row, mats), rec["part_number"]


def test_self_similarity_is_one(repos, history):
    mats, parts = repos
    rec = history[0]
    top = find_similar(rec["spec"], parts, mats)[0]
    assert top.part_id == rec["part_id"] and top.score == pytest.approx(1.0)


def test_exclude_part_id_leave_one_out(repos, history):
    mats, parts = repos
    for rec in history[:10]:
        sims = find_similar(rec["spec"], parts, mats, exclude_part_id=rec["part_id"])
        assert rec["part_id"] not in {s.part_id for s in sims}


def test_top_k_sorted_and_thresholded(repos, history):
    mats, parts = repos
    for rec in history[::9]:
        sims = find_similar(rec["spec"], parts, mats, top_k=5)
        assert len(sims) <= 5
        scores = [s.score for s in sims]
        assert scores == sorted(scores, reverse=True)
        assert all(s >= BIZ.similar_min_score for s in scores)
    assert len(find_similar(history[0]["spec"], parts, mats)) <= BIZ.similar_top_k


def test_breakdown_and_ref_fields(repos):
    mats, parts = repos
    sims = find_similar(gold("RFQ-2026-0102", "FL-2208", mats), parts, mats, qty=1000)
    s = sims[0]
    assert set(s.score_breakdown) == set(WEIGHTS) | {"shape"}
    assert s.score == pytest.approx(sum(WEIGHTS[k] * s.score_breakdown[k] for k in WEIGHTS), abs=1e-3)
    assert s.ref_unit_cost_eur and s.ref_unit_cost_eur > 0
    assert s.ref_qty == 1000 and s.ref_plant in BIZ.plants
    assert s.family == "flange" and s.material_code == "AW6082"


def test_shape_class_soft_penalty(repos):
    mats, _ = repos
    spec = gold("RFQ-2026-0102", "FL-2208", mats)
    f = featurize_spec(spec, mats)
    other = dict(f, shape_class=ShapeClass.PRISMATIC.value)
    same, _ = score_features(f, f)
    penalised, bd = score_features(f, other)
    assert same == pytest.approx(1.0)
    assert penalised == pytest.approx(0.3) and bd["shape"] == 0.3


def test_material_similarity_levels(repos):
    mats, _ = repos
    f = featurize_spec(gold("RFQ-2026-0101", "SH-4711", mats), mats)
    assert score_features(f, dict(f))[1]["material"] == 1.0
    assert score_features(f, dict(f, material_code="C45"))[1]["material"] == 0.7  # same group
    assert score_features(f, dict(f, material_code="AW6082", mat_group="aluminium"))[1]["material"] == 0.3
    unknown = dict(f, material_code=None, mat_group=None)
    assert score_features(unknown, f)[1]["material"] == 0.3


def test_reuse_hint_fires_for_flange(repos):
    mats, parts = repos
    spec = gold("RFQ-2026-0102", "FL-2208", mats)
    sims = find_similar(spec, parts, mats)
    issue = reuse_hint(spec, sims, line_no=3)
    assert issue is not None
    assert issue.code == "REUSE-001" and issue.severity == Severity.INFO and issue.line_no == 3
    assert "FL-2150" in issue.message and "FL-2150" in issue.suggestion
    assert "routing" in issue.suggestion and "inspection plan" in issue.suggestion
    assert KnowledgeBase.load().get(issue.kb_ref) is not None


def test_reuse_hint_requires_material_and_dimensions(repos):
    mats, parts = repos
    spec = gold("RFQ-2026-0102", "FL-2208", mats)
    sims = find_similar(spec, parts, mats)
    assert reuse_hint(spec, [], 1) is None
    other_mat = spec.model_copy(deep=True)
    other_mat.title_block.material_code = "C45"
    assert reuse_hint(other_mat, sims, 1) is None
    # same features but 6 % larger diameter: score stays high, the dimension criterion blocks reuse
    bigger = spec.model_copy(deep=True)
    bigger.envelope.max_diameter_mm = 125.5
    sims_big = find_similar(bigger, parts, mats)
    assert sims_big[0].part_number == "FL-2150" and sims_big[0].score >= 0.92
    assert reuse_hint(bigger, sims_big, 1) is None


def test_no_reuse_for_shaft(repos):
    mats, parts = repos
    spec = gold("RFQ-2026-0101", "SH-4711", mats)
    assert reuse_hint(spec, find_similar(spec, parts, mats), 1) is None
