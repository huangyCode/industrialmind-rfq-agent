"""Offline replay of the recorded extraction calls (no model server) + the deterministic feature repairs."""

from __future__ import annotations

import dataclasses

import pytest

from rfq_agent.agents.drawing import check_envelope, extract_drawing, repair_features
from rfq_agent.agents.intake import parse_rfq
from rfq_agent.config import LLM_CACHE_DIR, ROOT, LLMSettings
from rfq_agent.data.repositories import MaterialRepo
from rfq_agent.llm.client import get_client
from rfq_agent.models import Envelope, Evidence, Feature, FeatureType, ShapeClass, Tolerance

SAMPLES = ROOT / "data" / "samples"
RFQ_DIRS = sorted(SAMPLES.glob("RFQ-*"))
DRAWINGS = sorted(SAMPLES.glob("*/drawings/*.pdf"))  # tuning set + data/samples/holdout
HOLDOUT = sorted((SAMPLES / "holdout" / "drawings").glob("*.pdf"))
has_cache = LLM_CACHE_DIR.is_dir() and any(LLM_CACHE_DIR.rglob("*.json"))
needs_cache = pytest.mark.skipif(not has_cache, reason="fixtures/llm_cache is empty")


@pytest.fixture(scope="module")
def replay_client():
    # a closed port: any attempt to reach a model fails, so every answer must come from the cache
    s = dataclasses.replace(
        LLMSettings.from_env(),
        provider="ollama",
        model="",
        vision_model="",
        mode="replay",
        cache_dir="",
        ollama_base_url="http://127.0.0.1:9",
    )
    return get_client(s, call_repo=None)


@needs_cache
def test_sample_set_complete():
    assert len(RFQ_DIRS) == 4 and len(DRAWINGS) == 8 and len(HOLDOUT) == 3


@needs_cache
@pytest.mark.parametrize("rfq_dir", RFQ_DIRS, ids=lambda p: p.name)
def test_parse_rfq_replays_from_cache(rfq_dir, replay_client):
    req = parse_rfq(rfq_dir, replay_client)
    assert req.rfq_id == rfq_dir.name
    assert req.items and all(i.drawing_ref for i in req.items)
    assert replay_client.trace[-1]["cache_hit"] is True


@needs_cache
@pytest.mark.parametrize("pdf", DRAWINGS, ids=lambda p: p.stem)
def test_extract_drawing_replays_from_cache(pdf, replay_client, db):
    n0 = len(replay_client.trace)
    spec, pages = extract_drawing(pdf, replay_client, MaterialRepo(db))
    calls = replay_client.trace[n0:]
    assert [c["node"] for c in calls] == ["drawing_title_block", "drawing_notes", "drawing_views"]
    assert all(c["cache_hit"] for c in calls)
    assert spec.drawing_file == pdf.name and pages
    assert spec.title_block.part_number == pdf.stem
    if spec.envelope.shape_class == ShapeClass.ASSEMBLY:
        assert spec.parts_list and not spec.features
    else:
        assert spec.features


@needs_cache
def test_model_only_scoring_skips_repairs(replay_client, db):
    """eval_extraction's model-only mode returns the raw model features and restores the repairs afterwards."""
    import sys

    sys.path.insert(0, str(ROOT / "eval"))
    import eval_extraction as ev

    from rfq_agent.agents import drawing

    pdf = next(p for p in DRAWINGS if p.stem == "GR-3340")
    raw = ev.extract_model_only(pdf, replay_client, MaterialRepo(db))
    assert drawing.repair_features is repair_features and drawing.check_envelope is check_envelope
    fixed, _ = extract_drawing(pdf, replay_client, MaterialRepo(db))
    assert raw.features and fixed.features
    assert not any(
        "added from the envelope" in w or "Dropped untoleranced" in w for w in raw.extraction_warnings
    )


# ---------- deterministic repairs ----------


def _f(type_, nominal, text, fit=None, **kw) -> Feature:
    return Feature(
        id="F9",
        type=type_,
        description=text,
        nominal_mm=nominal,
        length_mm=kw.pop("length_mm", None),
        tolerance=Tolerance(upper=None, lower=None, fit=fit) if fit else None,
        ra_um=None,
        thread_spec=None,
        gear_module=None,
        gear_teeth=None,
        face_width_mm=None,
        evidence=Evidence(text=text, location="view"),
        **kw,
    )


def _env(shape, d=None, length=None) -> Envelope:
    return Envelope(shape_class=shape, max_diameter_mm=d, length_mm=length, width_mm=None, height_mm=None)


def test_fit_case_decides_shaft_vs_hole():
    out, _ = repair_features(
        [_f(FeatureType.BORE, 32, "Ø32 h7", "h7"), _f(FeatureType.OUTER_DIAMETER, 25, "Ø25 H7", "H7")],
        _env(ShapeClass.ROTATIONAL, 32),
    )
    assert [f.type for f in out] == [FeatureType.OUTER_DIAMETER, FeatureType.BORE]


def test_slot_fit_without_diameter_is_keyway():
    out, _ = repair_features([_f(FeatureType.BORE, 8, "8 jS9", "jS9")], _env(ShapeClass.ROTATIONAL))
    assert out[0].type == FeatureType.KEYWAY and out[0].tolerance.fit == "JS9"
    out, _ = repair_features([_f(FeatureType.BORE, 25, "25 H7", "H7")], _env(ShapeClass.ROTATIONAL))
    assert out[0].type == FeatureType.BORE  # H7 is not a key-slot class


def test_prismatic_bore_becomes_hole_and_pocket_width_is_smaller_side():
    out, _ = repair_features(
        [_f(FeatureType.BORE, 6, "Ø6 depth 80"), _f(FeatureType.POCKET, 90, "POCKET 90x35 depth 12")],
        _env(ShapeClass.PRISMATIC),
    )
    assert out[0].type == FeatureType.HOLE
    assert (out[1].nominal_mm, out[1].length_mm) == (35, 90)


def test_untoleranced_face_dropped_and_max_diameter_added():
    out, warnings = repair_features(
        [_f(FeatureType.FACE, 20, "20"), _f(FeatureType.BORE, 30, "Ø30 H7", "H7")],
        _env(ShapeClass.ROTATIONAL, 100, 20),
    )
    assert [(f.type, f.nominal_mm) for f in out] == [
        (FeatureType.OUTER_DIAMETER, 100),
        (FeatureType.BORE, 30),
    ]
    assert [f.id for f in out] == ["F1", "F2"] and len(warnings) == 2


def test_envelope_that_cannot_fit_the_sheet_is_nulled():
    env, w = check_envelope(_env(ShapeClass.ASSEMBLY, 90, 1230), "1:1")
    assert env.length_mm is None and env.max_diameter_mm == 90 and w
    env, w = check_envelope(_env(ShapeClass.ROTATIONAL, 90, 1230), "1:5")
    assert env.length_mm == 1230 and not w
