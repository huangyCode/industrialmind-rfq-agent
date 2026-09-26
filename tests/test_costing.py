from __future__ import annotations

from datetime import date

import pytest

from rfq_agent.agents.costing import compute_costs, price_deviation, recommend_plant
from rfq_agent.config import BIZ
from rfq_agent.data.repositories import RateRepo, ServiceRepo
from rfq_agent.models import BOMLine, CostBreakdown, MakeOrBuy, RoutingOp, SimilarPart

TODAY = date(2026, 9, 26)


def op(seq, wc, setup, cycle, **kw) -> RoutingOp:
    return RoutingOp(
        seq=seq,
        op_code=wc,
        work_center=wc,
        description=kw.pop("description", wc.replace("_", " ").title()),
        setup_min=setup,
        cycle_min=cycle,
        basis=kw.pop("basis", "rule"),
        rule_ids=kw.pop("rule_ids", ["R02"]),
        confidence=kw.pop("confidence", 0.8),
        **kw,
    )


def ht(seq=50, code="HT_QT") -> RoutingOp:
    return op(seq, "HEAT_TREAT", 0, 0, outsourced=True, service_code=code, rule_ids=["R07"])


def raw(kg=1.5, eur_kg=2.40, code="42CrMo4") -> BOMLine:
    return BOMLine(
        item_no=1,
        part_number=code,
        description=f"Round bar {code} Ø45 × 225 mm",
        qty_per=kg,
        unit="kg",
        make_or_buy=MakeOrBuy.BUY,
        source="raw_material",
        matched_ref=code,
        match_method="rule",
        unit_cost_eur=eur_kg,
        confidence=1.0,
    )


@pytest.fixture(scope="module")
def repos(db):
    return RateRepo(db), ServiceRepo(db).all


def run(repos, routing, bom, quantities, weight=1.2, due=None):
    rates, services = repos
    return compute_costs(
        routing=routing,
        bom=bom,
        quantities=quantities,
        finished_weight_kg=weight,
        requested_delivery=due,
        today=TODAY,
        rates=rates,
        services=services,
    )


def pick(costs: list[CostBreakdown], plant: str, qty: int) -> CostBreakdown:
    return next(c for c in costs if c.plant == plant and c.qty == qty)


def test_hand_computed_simple_part(repos):
    costs = run(repos, [op(10, "CNC_TURN", 30, 4.2)], [raw()], [200])
    de = pick(costs, "DE", 200)
    material = 1.5 * 2.40 * 1.03
    machining = (30 / 200 + 4.2) / 60 * 95
    subtotal = material + machining
    unit_cost = subtotal * (1 + 0.12 + 0.01)
    unit_price = unit_cost / (1 - 0.18)
    assert de.feasible
    assert abs(de.unit_cost_eur - unit_cost) < 0.01
    assert abs(de.unit_price_eur - unit_price) < 0.01
    assert abs(de.total_price_eur - unit_price * 200) < 0.01
    assert abs(sum(line.amount_eur_per_pc for line in de.lines) - de.unit_price_eur) < 1e-9
    mach = next(line for line in de.lines if line.category == "machining")
    assert mach.formula == "(30/200 + 4.20) min / 60 × 95.00 €/h"
    assert mach.source == "rates:DE/CNC_TURN"
    mat = next(line for line in de.lines if line.category == "material")
    assert mat.source == "materials:42CrMo4" and "2.40 €/kg" in mat.formula
    assert {line.source for line in de.lines if line.category in ("overhead", "logistics")} == {"plants:DE"}
    assert len(costs) == len(BIZ.plants)


def test_plant_without_grind_cyl_infeasible(repos):
    routing = [op(10, "CNC_TURN", 30, 4.2), op(20, "GRIND_CYL", 25, 2.0, rule_ids=["R08"])]
    costs = run(repos, routing, [raw()], [200])
    cn = pick(costs, "CN", 200)
    assert not cn.feasible
    assert "GRIND_CYL" in cn.infeasible_reason and "CN" in cn.infeasible_reason
    assert pick(costs, "DE", 200).feasible and pick(costs, "PL", 200).feasible


def test_suggested_op_ignored_for_cost_and_feasibility(repos):
    routing = [op(10, "CNC_TURN", 30, 4.2), op(20, "GRIND_CYL", 25, 2.0, suggested=True)]
    costs = run(repos, routing, [raw()], [200])
    base = run(repos, [op(10, "CNC_TURN", 30, 4.2)], [raw()], [200])
    assert pick(costs, "CN", 200).feasible
    assert pick(costs, "DE", 200).unit_price_eur == pytest.approx(pick(base, "DE", 200).unit_price_eur)


def test_unit_price_decreases_with_quantity(repos):
    routing = [op(10, "SAW", 5, 0.8), op(20, "CNC_TURN", 40, 4.2), ht()]
    costs = run(repos, routing, [raw()], [10, 50, 200, 1000])
    for plant in BIZ.plants:
        prices = [pick(costs, plant, q).unit_price_eur for q in (10, 50, 200, 1000)]
        assert prices == sorted(prices, reverse=True) and prices[0] > prices[-1]


def test_min_order_floor(repos):
    costs = run(repos, [op(10, "CNC_TURN", 10, 1.0)], [raw(0.2)], [2, 500])
    small = pick(costs, "DE", 2)
    assert small.unit_price_eur * 2 < BIZ.min_order_value_eur
    assert small.min_order_applied and small.total_price_eur == BIZ.min_order_value_eur
    big = pick(costs, "DE", 500)
    assert not big.min_order_applied and big.total_price_eur == pytest.approx(big.unit_price_eur * 500)


def test_outsourced_lot_minimum_dominates_low_qty(repos):
    _, services = repos
    weight = 1.2
    costs = run(repos, [op(10, "CNC_TURN", 30, 4.2), ht()], [raw()], [10, 1000], weight=weight)
    low = next(line for line in pick(costs, "DE", 10).lines if line.category == "outsourced")
    high = next(line for line in pick(costs, "DE", 1000).lines if line.category == "outsourced")
    svc = services["HT_QT"]
    assert low.amount_eur_per_pc == pytest.approx(svc["lot_min_eur"] / 10)
    assert high.amount_eur_per_pc == pytest.approx(svc["price_eur_kg"] * weight)
    assert low.source == "services:HT_QT"


def test_lead_time_and_due_date(repos):
    routing = [op(10, "CNC_TURN", 30, 4.2), ht()]
    due = date(2026, 11, 20)  # ~8 weeks out
    costs = run(repos, routing, [raw()], [200], due=due)
    de, pl, cn = (pick(costs, p, 200) for p in ("DE", "PL", "CN"))
    assert de.lead_time_weeks == 3 + BIZ.outsourced_extra_weeks
    assert cn.lead_time_weeks == 9 + BIZ.outsourced_extra_weeks
    assert de.meets_due_date and pl.meets_due_date and cn.meets_due_date is False
    # CN is cheapest but late -> PL (cheapest on-time)
    assert cn.unit_price_eur < pl.unit_price_eur < de.unit_price_eur
    assert recommend_plant(costs, 200) == "PL"
    no_due = run(repos, routing, [raw()], [200])
    assert pick(no_due, "DE", 200).meets_due_date is None
    assert recommend_plant(no_due, 200) == "CN"


def test_recommend_tie_prefers_de_and_fallback():
    def cb(plant, price, meets=True, feasible=True):
        return CostBreakdown(
            plant=plant, qty=100, feasible=feasible, unit_price_eur=price, meets_due_date=meets
        )

    assert recommend_plant([cb("PL", 10.0), cb("DE", 10.001)], 100) == "DE"
    assert recommend_plant([cb("PL", 10.0, meets=False), cb("CN", 8.0, meets=False)], 100) == "CN"
    assert recommend_plant([cb("DE", 1.0, feasible=False)], 100) is None


def test_assembly_bom_lines_and_unpriced(repos):
    bom = [
        BOMLine(
            item_no=1,
            part_number="6204-2RS",
            description="Bearing",
            qty_per=2,
            make_or_buy=MakeOrBuy.BUY,
            source="catalog",
            matched_ref="6204-2RS",
            match_method="exact",
            unit_cost_eur=2.80,
            confidence=1.0,
        ),
        BOMLine(
            item_no=2,
            part_number="HS-2001",
            description="Housing",
            qty_per=1,
            make_or_buy=MakeOrBuy.MAKE,
            source="internal",
            matched_ref="HS-2001",
            match_method="exact",
            unit_cost_eur=40.0,
            confidence=0.9,
        ),
        BOMLine(
            item_no=3,
            part_number="EC-5103",
            description="End cover",
            qty_per=1,
            make_or_buy=MakeOrBuy.MAKE,
            source="new",
            match_method="none",
            confidence=0.3,
        ),
    ]
    costs = run(repos, [op(10, "ASSEMBLY", 20, 6.0, rule_ids=["R12"])], bom, [100])
    de = pick(costs, "DE", 100)
    cats = {line.category: line for line in de.lines}
    assert cats["purchased"].amount_eur_per_pc == pytest.approx(5.6)
    new = [line for line in de.lines if "price on request" in line.description]
    assert len(new) == 1 and new[0].amount_eur_per_pc == 0 and de.feasible


class FakeParts:
    """Minimal PartRepo stand-in (history tables may be empty in the test DB)."""

    def __init__(self, row, costs, quotes=()):
        self.row, self._costs, self._quotes = row, list(costs), list(quotes)

    def get(self, part_id):
        return self.row

    def costs(self, part_id):
        return self._costs

    def quotes(self, part_id):
        return self._quotes


def _sim(score=0.9):
    return SimilarPart(
        part_id=7,
        part_number="SH-1007",
        title="Output shaft",
        family="shaft",
        score=score,
        score_breakdown={},
        material_code="42CrMo4",
    )


def test_price_deviation_scaled_and_same_plant():
    costs = [CostBreakdown(plant="DE", qty=200, feasible=True, unit_cost_eur=12.0, unit_price_eur=14.63)]
    row = {"max_diameter_mm": 40.0, "length_mm": 200.0, "width_mm": None, "height_mm": None}
    ref_vol = 3.141592653589793 / 4 * 40 * 40 * 200 / 1000
    parts = FakeParts(
        row,
        [
            {"plant": "DE", "qty": 200, "unit_cost_eur": 10.0},
            {"plant": "DE", "qty": 1000, "unit_cost_eur": 7.0},
            {"plant": "CN", "qty": 200, "unit_cost_eur": 5.0},
        ],
    )
    new_vol = ref_vol * 8  # scale = 4
    dev = price_deviation(costs, "DE", 200, [_sim()], new_vol, parts)
    assert dev == pytest.approx((12.0 - 40.0) / 40.0)
    # quote fallback compares price with price
    parts_q = FakeParts(row, [], [{"plant": "DE", "qty": 150, "unit_price_eur": 14.0}])
    assert price_deviation(costs, "DE", 200, [_sim()], ref_vol, parts_q) == pytest.approx(0.63 / 14.0)
    assert price_deviation(costs, "DE", 200, [], ref_vol, parts) is None
    assert price_deviation(costs, None, 200, [_sim()], ref_vol, parts) is None
    assert price_deviation(costs, "DE", 200, [_sim()], ref_vol, FakeParts(row, [])) is None


def _internal(item_no, pn, cost, plant=None) -> BOMLine:
    return BOMLine(
        item_no=item_no,
        part_number=pn,
        description="Make part",
        qty_per=1,
        make_or_buy=MakeOrBuy.MAKE,
        source="internal",
        matched_ref=pn,
        match_method="exact",
        unit_cost_eur=cost,
        confidence=0.85,
        note=f"historical cost {plant} @ qty 200" if plant else "internal part",
    )


def test_assembly_recommends_make_part_plant(repos):
    """quoting_policy.md#assemblies: built where the make parts come from, not simply the cheapest plant."""
    asm = [op(10, "ASSEMBLY", 20, 6.0, rule_ids=["R12"])]
    # both make parts costed at PL → PL, although it is not the cheapest plant for this assembly
    costs = run(repos, asm, [_internal(1, "HS-1", 4.0, "PL"), _internal(2, "SH-2", 2.0, "PL")], [200])
    cheapest = min((c for c in costs if c.qty == 200), key=lambda c: c.unit_price_eur).plant
    assert cheapest != "PL"
    src = [ln.source for ln in pick(costs, "DE", 200).lines if ln.category == "internal_parts"]
    assert src == ["parts:HS-1@PL", "parts:SH-2@PL"]
    assert recommend_plant(costs, 200) == "PL"
    # mixed plants or a make part without a known plant → DE
    mixed = run(repos, asm, [_internal(1, "HS-1", 40.0, "CN"), _internal(2, "SH-2", 20.0, "PL")], [200])
    assert recommend_plant(mixed, 200) == "DE"
    unknown = run(repos, asm, [_internal(1, "HS-1", 40.0)], [200])
    assert recommend_plant(unknown, 200) == "DE"
    # purchased parts only → normal cheapest-plant logic; all plants stay in the price table
    bought = run(repos, asm, [], [200])
    assert recommend_plant(bought, 200) == min(bought, key=lambda c: c.unit_price_eur).plant
    assert {c.plant for c in mixed} == set(BIZ.plants)
