from __future__ import annotations

import pytest

from rfq_agent.agents.confidence import assess
from rfq_agent.config import BIZ
from rfq_agent.models import (
    BOMLine,
    DrawingSpec,
    Envelope,
    Evidence,
    Feature,
    FeatureType,
    MakeOrBuy,
    PartsListItem,
    RFQItem,
    RoutingOp,
    Severity,
    ShapeClass,
    SimilarPart,
    Tier,
    TitleBlock,
    ValidationIssue,
)


def ev(t):
    return Evidence(text=t, location="title block")


def shaft_spec(**kw) -> DrawingSpec:
    tb = TitleBlock(
        part_number="SH-4711",
        revision="B",
        title="Output shaft",
        material="42CrMo4+QT",
        evidence={
            "part_number": ev("SH-4711"),
            "revision": ev("B"),
            "title": ev("Output shaft"),
            "material": ev("42CrMo4+QT"),
        },
    )
    feats = [
        Feature(
            id="F1",
            type=FeatureType.OUTER_DIAMETER,
            description="Ø38 k6",
            nominal_mm=38,
            evidence=Evidence(text="Ø38 k6"),
        ),
        Feature(
            id="F2", type=FeatureType.KEYWAY, description="Keyway 10 P9", evidence=Evidence(text="10 P9")
        ),
    ]
    data = dict(
        drawing_file="shaft.pdf",
        title_block=tb,
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=45, length_mm=220),
        features=feats,
    )
    data.update(kw)
    return DrawingSpec(**data)


ITEM = RFQItem(line_no=1, drawing_ref="shaft.pdf", quantities=[50, 200])


def sim(score):
    return SimilarPart(
        part_id=3,
        part_number="SH-1003",
        title="Shaft",
        family="shaft",
        score=score,
        score_breakdown={},
        material_code="42CrMo4",
    )


def blended_op():
    return RoutingOp(
        seq=10,
        op_code="TURN",
        work_center="CNC_TURN",
        description="Turning",
        setup_min=30,
        cycle_min=4,
        basis="blend",
        rule_ids=["R02"],
        ref_part_id=3,
        blend_weight=0.8,
        confidence=0.85,
    )


def issue(sev, code="VAL-001"):
    return ValidationIssue(code=code, severity=sev, line_no=1, message=f"{code} test", suggestion="fix")


def run(spec=None, issues=(), similar=None, uncovered=(), bom=(), dev=0.05, routing=None):
    return assess(
        spec=spec or shaft_spec(),
        item=ITEM,
        issues=list(issues),
        similar=[sim(0.95)] if similar is None else similar,
        routing=[blended_op()] if routing is None else routing,
        uncovered=list(uncovered),
        bom=list(bom),
        price_deviation_pct=dev,
    )


def test_clean_high_similarity_fast_track():
    r = run()
    assert r.extraction == pytest.approx(1.0)
    assert r.overall == pytest.approx(0.35 + 0.25 * 0.95 + 0.20 + 0.20)
    assert r.tier == Tier.FAST_TRACK
    assert r.bom_match is None


def test_blocker_zero_and_manual():
    r = run(issues=[issue(Severity.BLOCKER, "VAL-003")])
    assert r.overall == 0 and r.tier == Tier.MANUAL
    assert any("BLOCKER VAL-003" in x for x in r.reasons)


def test_warning_not_fast_track():
    r = run(issues=[issue(Severity.WARNING, "DFM-002")])
    assert r.overall >= BIZ.fast_track_threshold
    assert r.tier == Tier.STANDARD
    assert any("DFM-002" in x for x in r.reasons)


def test_deductions_have_reasons():
    tb = shaft_spec().title_block.model_copy(update={"evidence": {"part_number": ev("SH-4711")}})
    spec = shaft_spec(title_block=tb, extraction_warnings=["envelope length != sum of sections"])
    r = run(spec=spec, similar=[], uncovered=["F2"], dev=0.45)
    assert r.similarity == 0.3 and r.coverage == 0.5 and r.price_sanity == 0.4
    # completeness 1, evidence 3/6 (revision/title/material lack evidence), 1 inconsistency
    assert r.extraction == pytest.approx(0.5 * 0.9)
    text = " | ".join(r.reasons)
    for needle in ("material", "No similar", "F2 (Keyway 10 P9)", "+45%", "consistency"):
        assert needle in text
    assert r.tier == Tier.MANUAL


def test_price_sanity_bands():
    assert run(dev=0.10).price_sanity == 1.0
    assert run(dev=-0.25).price_sanity == 0.7
    assert run(dev=0.31).price_sanity == 0.4
    assert run(dev=None).price_sanity == 0.6


def test_assembly_uses_bom_match():
    spec = shaft_spec(
        envelope=Envelope(shape_class=ShapeClass.ASSEMBLY, length_mm=300),
        features=[],
        parts_list=[PartsListItem(item_no=1, description="Housing", quantity=1)],
    )
    bom = [
        BOMLine(
            item_no=1,
            part_number="6204-2RS",
            description="Bearing",
            qty_per=2,
            make_or_buy=MakeOrBuy.BUY,
            source="catalog",
            match_method="exact",
            unit_cost_eur=2.8,
            confidence=1,
        ),
        BOMLine(
            item_no=2,
            part_number="EC-5103",
            description="End cover",
            qty_per=1,
            make_or_buy=MakeOrBuy.MAKE,
            source="new",
            match_method="none",
            confidence=0.3,
        ),
    ]
    r = run(spec=spec, bom=bom)
    assert r.bom_match == 0.5
    exp = 0.30 * r.extraction + 0.20 * 0.95 + 0.15 * 1.0 + 0.15 * 1.0 + 0.20 * 0.5
    assert r.overall == pytest.approx(exp)
    assert any("EC-5103" in x for x in r.reasons)


def test_fast_track_needs_close_precedent_and_confirmed_price():
    # Scores high overall, but the precedent is only moderately similar -> engineer must look.
    r = run(similar=[sim(0.80)], dev=0.05)
    assert r.tier == Tier.STANDARD
    assert any("no close precedent" in x for x in r.reasons)
    # Close precedent but price not confirmed by it -> also STANDARD.
    r = run(similar=[sim(0.95)], dev=None)
    assert r.tier == Tier.STANDARD
    assert any("price not confirmed" in x for x in r.reasons)


def test_suggested_op_reason_names_op_code_and_part_number():
    suggested = RoutingOp(
        seq=35,
        op_code="STRAIGHTEN",
        work_center="INSPECT",
        description="Straighten",
        setup_min=5,
        cycle_min=2,
        basis="blend",
        rule_ids=["CAL"],
        ref_part_id=3,
        suggested=True,
        confidence=0.6,
    )
    r = run(routing=[blended_op(), suggested])
    assert "Suggested op STRAIGHTEN from reference part SH-1003 (not costed)" in r.reasons
    assert not any("INSPECT" in x or "part 3" in x for x in r.reasons)
