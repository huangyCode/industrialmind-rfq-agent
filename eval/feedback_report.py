"""Reviewer feedback per work center (DESIGN §9, §5.10): how far engineers correct the planned times.

    uv run python eval/feedback_report.py

Reads feedback through `app_api` (`RFQService.feedback()` → `FeedbackRecord.diff`, the field-level
before/after of each edit round) from two sources:
    recorded  the feedback table of data/rfq.db (read-only) — whatever real review rounds were run
    demo      a scripted sample: three review rounds on RFQ-0101/0102/0104 run through app_api on a
              private copy of the database (reference extractor, template prose). Labelled as demo data.
Writes eval/reports/feedback.md.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from eval_routing import TODAY, isolated_service  # noqa: E402

from rfq_agent.app_api import RFQService  # noqa: E402
from rfq_agent.config import DB_PATH, SAMPLES_DIR  # noqa: E402
from rfq_agent.graph.deps import Deps, ReferenceExtractor  # noqa: E402
from rfq_agent.models import FeedbackRecord, ReviewDecision, ReviewEdit  # noqa: E402

REPORT = ROOT / "eval" / "reports" / "feedback.md"
_PATH = re.compile(r"routing\[seq=(\d+),([^\]]+)\]\.(\w+)")
TIME_FIELDS = ("cycle_min", "setup_min")

# (rfq, reviewer, comment, [(op_code, field, factor or None)]) — factor multiplies the current value;
# field "accept_suggested" accepts the suggested op with that op_code.
DEMO_ROUNDS = [
    (
        "RFQ-2026-0101",
        "M. Keller (demo)",
        "Grinding two k6 seats at Ra 0.8 takes longer than planned; straightening is needed at L/D > 4.5.",
        [("GRIND", "cycle_min", 1.12), ("STRAIGHTEN", "accept_suggested", None)],
    ),
    (
        "RFQ-2026-0102",
        "S. Novak (demo)",
        "Programme and fixture of the reference flange exist: shorter setup, slightly faster turning.",
        [("TURN", "setup_min", 0.5), ("TURN", "cycle_min", 0.9)],
    ),
    (
        "RFQ-2026-0104",
        "M. Keller (demo)",
        "Seal and bearing press-fit need more time than the standard assembly rate.",
        [("ASSEMBLY", "cycle_min", 1.25)],
    ),
]


# ---------- sources ----------


def recorded_feedback(db: Path = DB_PATH) -> list[FeedbackRecord]:
    """Feedback already in the real database, via app_api; the database is opened read-only."""
    if not Path(db).exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with tempfile.TemporaryDirectory(prefix="rfq-fb-") as tmp:
        svc = RFQService(Deps.from_conn(conn, client=None), Path(tmp) / "cp.db")
        return svc.feedback()


def _op(view, op_code: str) -> dict:
    for op in view.lines[0]["routing"]:
        if op["op_code"] == op_code:
            return op
    raise LookupError(f"{view.rfq_id}: no op {op_code}")


def demo_feedback(workdir: Path) -> list[FeedbackRecord]:
    """Script DEMO_ROUNDS through app_api on a private DB copy; each round = edit → recompute → approve."""
    svc = isolated_service(workdir, {"reference": ReferenceExtractor()})
    svc.deps.feedback.conn.execute("DELETE FROM feedback")  # the copy starts with the recorded rows
    svc.deps.feedback.conn.commit()
    for rfq_id, reviewer, comment, changes in DEMO_ROUNDS:
        view = svc.start(SAMPLES_DIR / rfq_id, "reference")
        edits = []
        for op_code, field, factor in changes:
            op = _op(view, op_code)
            value = None if factor is None else round(op[field] * factor, 2)
            edits.append(ReviewEdit(line_no=1, target="routing", seq=op["seq"], field=field, new_value=value))
        view = svc.review(
            rfq_id, ReviewDecision(action="edit", reviewer=reviewer, edits=edits, comment=comment)
        )
        svc.review(rfq_id, ReviewDecision(action="approve", reviewer=reviewer))
    return svc.feedback()


# ---------- aggregation ----------


def time_edits(records: list[FeedbackRecord]) -> list[dict]:
    """One row per changed routing field (from FeedbackRecord.diff), with work center and ratios."""
    out = []
    for r in records:
        before_ops = {o["seq"]: o for o in r.before.get("routing", [])}
        after_ops = {o["seq"]: o for o in r.after.get("routing", [])}
        plant = r.after.get("plant")
        prices = {d["path"]: d for d in r.diff if d["path"].startswith("unit_price[")}
        mine = [p for k, p in prices.items() if plant and k.startswith(f"unit_price[{plant}@")]
        # smallest quantity of the recommended plant, e.g. "unit_price[DE@50]"
        price = min(mine, key=lambda p: int(p["path"].split("@")[1].rstrip("]")), default=None)
        for d in r.diff:
            m = _PATH.fullmatch(d["path"])
            if not m:
                continue
            seq, op_code, field = int(m.group(1)), m.group(2), m.group(3)
            if field not in (*TIME_FIELDS, "suggested"):
                continue
            op = after_ops.get(seq) or before_ops.get(seq) or {}
            row = {
                "rfq_id": r.rfq_id,
                "reviewer": r.reviewer,
                "op_code": op_code,
                "work_center": op.get("work_center", "?"),
                "field": field,
                "before": d["before"],
                "after": d["after"],
                "ratio": None,
                "rule_ratio": None,
                "basis_before": (before_ops.get(seq) or {}).get("basis"),
                "price_before": price["before"] if price else None,
                "price_after": price["after"] if price else None,
                "price_at": price["path"][len("unit_price[") : -1] if price else None,
            }
            if field in TIME_FIELDS and d["before"]:
                row["ratio"] = d["after"] / d["before"]
                rule = (before_ops.get(seq) or {}).get("rule_cycle_min")
                if field == "cycle_min" and rule:
                    row["rule_ratio"] = d["after"] / rule
            elif field == "suggested":
                row["field"] = "accept_suggested"
                row["before"], row["after"] = "suggested", "accepted (costed)"
            out.append(row)
    # routing ops added / removed by the reviewer (diff path without a field)
    for r in records:
        before_ops = {o["seq"]: o for o in r.before.get("routing", [])}
        after_ops = {o["seq"]: o for o in r.after.get("routing", [])}
        for d in r.diff:
            m = re.fullmatch(r"routing\[seq=(\d+)\]", d["path"])
            if m:
                seq = int(m.group(1))
                op = after_ops.get(seq) or before_ops.get(seq) or {}
                out.append(
                    {
                        "rfq_id": r.rfq_id,
                        "reviewer": r.reviewer,
                        "op_code": d["after"] or d["before"],
                        "work_center": op.get("work_center", "?"),
                        "field": "add_op" if d["after"] else "remove_op",
                        "before": d["before"],
                        "after": d["after"],
                        "ratio": None,
                        "rule_ratio": None,
                        "basis_before": op.get("basis"),
                        "price_before": None,
                        "price_after": None,
                    }
                )
    return out


def by_work_center(rows: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"edits": 0, "cycle": [], "setup": [], "rule": [], "ops": 0})
    for x in rows:
        a = agg[x["work_center"]]
        a["edits"] += 1
        if x["field"] == "cycle_min" and x["ratio"] is not None:
            a["cycle"].append(x["ratio"])
            if x["rule_ratio"] is not None:
                a["rule"].append(x["rule_ratio"])
        elif x["field"] == "setup_min" and x["ratio"] is not None:
            a["setup"].append(x["ratio"])
        elif x["field"] in ("accept_suggested", "add_op", "remove_op"):
            a["ops"] += 1
    return dict(sorted(agg.items()))


# ---------- report ----------


def _mean(xs: list[float]) -> str:
    return f"{statistics.fmean(xs):.2f}" if xs else "–"


def _section(title: str, records: list[FeedbackRecord], intro: str) -> list[str]:
    rows = time_edits(records)
    lines = [f"## {title}", "", intro, ""]
    if not rows:
        return lines + ["_No routing edits recorded._", ""]
    wc = by_work_center(rows)
    lines += [
        f"{len(records)} feedback record(s) (one per edited line and review round), {len(rows)} routing change(s).",
        "",
        "| Work center | Edits | Mean cycle ratio (manual / before) | Mean cycle ratio vs rule time | "
        "Mean setup ratio | Ops accepted / added / removed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for k, a in wc.items():
        lines.append(
            f"| {k} | {a['edits']} | {_mean(a['cycle'])} | {_mean(a['rule'])} | {_mean(a['setup'])} | {a['ops']} |"
        )
    lines += [
        "",
        "| RFQ | Reviewer | Op | Work center | Change | Before → after | Ratio | Basis before | "
        "Unit price, recommended plant, smallest qty (effect of the whole round) |",
        "|---|---|---|---|---|---|---:|---|---|",
    ]
    for x in rows:
        ratio = f"{x['ratio']:.2f}" if x["ratio"] is not None else "–"
        price = (
            f"{x['price_at']}: €{x['price_before']} → €{x['price_after']}"
            if x["price_before"] is not None
            else "–"
        )
        lines.append(
            f"| {x['rfq_id']} | {x['reviewer']} | {x['op_code']} | {x['work_center']} | {x['field']} | "
            f"{x['before']} → {x['after']} | {ratio} | {x['basis_before'] or '–'} | {price} |"
        )
    return lines + [""]


def render(recorded: list[FeedbackRecord], demo: list[FeedbackRecord]) -> str:
    lines = [
        "# Reviewer feedback per work center",
        "",
        "_Generated by `eval/feedback_report.py` (`make eval`). Every edit round in the review workbench "
        "is stored as a feedback record: the edits, a before/after snapshot of routing, plant, tier and "
        "prices, and the field-level diff (`FeedbackRecord.diff`). This report aggregates the routing time "
        "changes per work center._",
        "",
        "**Correction ratio** = value set by the engineer / value the system proposed (the blended or "
        "rule time shown in review). 1.00 = no change; 1.20 = the engineer needed 20 % more time. The "
        "second ratio compares with the pure rule time (`rule_cycle_min`), which is what a rule coefficient "
        "would be corrected against.",
        "",
    ]
    lines += _section(
        "Recorded feedback (data/rfq.db)",
        recorded,
        "Review rounds that were actually run against the project database (e.g. the CLI demo).",
    )
    lines += _section(
        "Demo sample — scripted, not real reviewer data",
        demo,
        "**Demo data.** Three review rounds scripted through `app_api` on a private copy of the database so "
        "the report shows its shape: RFQ-0101 grinding +12 % and the suggested STRAIGHTEN op accepted; "
        "RFQ-0102 setup halved (programme and fixture of the reference flange exist — quoting policy "
        "#part-reuse) and turning −10 %; RFQ-0104 assembly +25 %. The factors are illustrative choices, "
        "not measurements.",
    )
    lines += [
        "## How this recalibrates the rules in production (shadow mode)",
        "",
        "A handful of records is far too few to change anything — the loop below is the design, not something this "
        "prototype has run.",
        "",
        "1. **Collect.** Every approved quote contributes its feedback records; MES actual times arrive "
        "later for the orders that are won. Both are joined per work center, op code, material group and "
        "part family.",
        "2. **Estimate a correction.** Per work center (and where the data allows, per family), the "
        "median of manual / rule time over a rolling window, e.g. last 6 months, at least 20 edits from "
        "at least 5 different parts and 2 engineers. The median and the per-part spread guard against one "
        "engineer's habits or one unusual part. Reuse-driven setup reductions (REUSE-001) are excluded from "
        "setup coefficients, because they describe the reference part, not the rule.",
        "3. **Shadow mode.** The corrected coefficient is applied in a shadow copy of the rule table: every "
        "new RFQ is costed with both, only the current table's price is shown. After some weeks compare "
        "both against what engineers finally approved and against MES actuals: MdAPE, bias, and the edit "
        "rate per tier (target: FAST_TRACK lines edited < 5 %).",
        "4. **Promote with a named approval.** If the shadow table is better on both, a process engineer "
        "approves the new coefficient (versioned rule table, change log). Old quotes stay reproducible "
        "because each quote stores the rule-table version.",
        "5. **Watch for drift.** If the edit rate or bias for a work center moves beyond a band, the work "
        "center is flagged for review (new machine, new crew, different material mix).",
        "",
        "The same records feed the confidence thresholds (DESIGN §5.9): the tier weights are tuned so "
        "that the edit rate per tier matches its promise, and the backtest (`backtest.md`) is re-run on "
        "the new history before any change goes live.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPORT)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="rfq-eval-feedback-") as tmp:
        recorded = recorded_feedback(args.db)
        demo = demo_feedback(Path(tmp) / "demo")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(recorded, demo), encoding="utf-8")
    for name, recs in (("recorded", recorded), ("demo", demo)):
        for k, a in by_work_center(time_edits(recs)).items():
            print(
                f"{name:<9} {k:<14} edits {a['edits']}  cycle ratio {_mean(a['cycle'])}  setup {_mean(a['setup'])}"
            )
    print(f"(today fixed at {TODAY}) → {args.out}")


if __name__ == "__main__":
    main()
