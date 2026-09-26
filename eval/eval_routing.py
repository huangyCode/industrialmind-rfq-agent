"""Routing (triage) check (DESIGN §9, §2): the 4 demo RFQs end-to-end through `app_api`, tier vs design.

    uv run python eval/eval_routing.py

Runs every sample twice on an isolated copy of the database, checkpoint store and quote directory
(nothing under data/ is written):
    (a) reference extractor — hand-written gold data for e-mail and drawing (isolates the deterministic chain)
    (b) LLM extractor in *replay* mode — the recorded model outputs in fixtures/llm_cache; never calls a model
        and never writes the cache. Skipped when no recordings exist.
Prose (cover letter / clarification e-mail) uses the built-in templates in both runs (no LLM).
Writes eval/reports/routing.md.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rfq_agent.app_api import RFQService, RunView  # noqa: E402
from rfq_agent.config import DB_PATH, LLM_CACHE_DIR, SAMPLES_DIR, LLMSettings  # noqa: E402
from rfq_agent.data.db import connect  # noqa: E402
from rfq_agent.graph.deps import Deps, LLMExtractor, ReferenceExtractor  # noqa: E402
from rfq_agent.models import RFQStatus, Tier  # noqa: E402

REPORT = ROOT / "eval" / "reports" / "routing.md"
TODAY = date(2026, 9, 26)  # fixed "today" so due-date checks are reproducible
EXPECTED: dict[str, Tier] = {  # DESIGN §2
    "RFQ-2026-0101": Tier.STANDARD,
    "RFQ-2026-0102": Tier.FAST_TRACK,
    "RFQ-2026-0103": Tier.MANUAL,  # blocker → clarification e-mail, no price
    "RFQ-2026-0104": Tier.STANDARD,
}
SHOWS = {
    "RFQ-2026-0101": "German e-mail; k6 seats → grinding; CN excluded (no grinder)",
    "RFQ-2026-0102": "near-identical historical part → REUSE-001 → one-click approval",
    "RFQ-2026-0103": "material missing → BLOCKER → clarification e-mail, no price",
    "RFQ-2026-0104": "assembly BOM; new part EC-5103 unpriced",
}


@dataclass
class Row:
    rfq_id: str
    extractor: str
    expected: Tier
    actual: str
    status: str
    confidence: float | None
    plant: str | None
    unit_price: str
    passed: bool
    note: str = ""


def isolated_service(workdir: Path, extractors: dict, client=None) -> RFQService:
    """RFQService on a private copy of data/rfq.db with its own checkpoints and quote dir."""
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "rfq.db"
    shutil.copy(DB_PATH, db)
    deps = Deps.from_conn(
        connect(db), client=client, extractors=extractors, quote_dir=workdir / "quotes", today=TODAY
    )
    return RFQService(deps, workdir / "checkpoints.db")


def replay_client(failure_dir: Path):
    """LLM client reading fixtures/llm_cache in replay mode (read-only), or None if nothing is recorded."""
    if not LLM_CACHE_DIR.exists() or not any(LLM_CACHE_DIR.rglob("*.json")):
        return None
    from rfq_agent.llm.cache import LLMCache
    from rfq_agent.llm.client import LLMClient
    from rfq_agent.llm.providers import make_provider

    s = LLMSettings.from_env()
    return LLMClient(
        make_provider(s),
        mode="replay",  # never calls a model, never writes the cache
        cache=LLMCache(LLM_CACHE_DIR),
        call_repo=None,
        failure_dir=failure_dir,
        max_tokens=s.max_tokens,
    )


def _row(view: RunView, extractor: str) -> Row:
    exp = EXPECTED[view.rfq_id]
    note = ""
    if view.error:
        return Row(
            view.rfq_id, extractor, exp, "ERROR", view.status, None, None, "–", False, view.error[:120]
        )
    if view.status == RFQStatus.NEEDS_CLARIFICATION.value:
        blockers = [i.code for i in view.issues if i.severity.value == "blocker"]
        q = len((view.clarification or {}).get("questions", []))
        return Row(
            view.rfq_id,
            extractor,
            exp,
            "manual (clarification)",
            view.status,
            0.0,
            None,
            "no price",
            exp == Tier.MANUAL,
            f"blockers {','.join(blockers)}; {q} question(s) in the e-mail",
        )
    ln = view.draft.lines[0]
    tier = ln.confidence.tier
    qty = ln.costs and min(c.qty for c in ln.costs)
    best = next((c for c in ln.costs if c.plant == ln.recommended_plant and c.qty == qty), None)
    price = f"€{best.unit_price_eur:,.2f} @ {qty}" if best else "–"
    if any(i.code == "REUSE-001" for i in ln.issues):
        note = "REUSE-001; "
    if view.auto_approved:
        note += "auto-approved; "
    infeasible = sorted({c.plant for c in ln.costs if not c.feasible})
    if infeasible:
        note += f"infeasible {','.join(infeasible)}; "
    unpriced = [b.part_number for b in ln.bom if b.unit_cost_eur is None and b.source != "service"]
    if unpriced:
        note += f"unpriced {','.join(p or '?' for p in unpriced)}; "
    return Row(
        view.rfq_id,
        extractor,
        exp,
        tier.value,
        view.status,
        ln.confidence.overall,
        ln.recommended_plant,
        price,
        tier == exp,
        note.rstrip("; "),
    )


def run(svc: RFQService, extractor: str) -> list[Row]:
    rows = []
    for rfq_id in EXPECTED:
        try:
            view = svc.start(SAMPLES_DIR / rfq_id, extractor)
        except Exception as e:  # report, do not crash the whole eval
            rows.append(
                Row(
                    rfq_id,
                    extractor,
                    EXPECTED[rfq_id],
                    "ERROR",
                    "error",
                    None,
                    None,
                    "–",
                    False,
                    repr(e)[:120],
                )
            )
            continue
        rows.append(_row(view, extractor))
    return rows


def evaluate(workdir: Path, *, llm: bool = True) -> dict:
    """{"reference": [Row], "llm": [Row] | None, "llm_stats": dict | None}."""
    ref = run(isolated_service(workdir / "reference", {"reference": ReferenceExtractor()}), "reference")
    out = {"reference": ref, "llm": None, "llm_stats": None}
    if llm:
        client = replay_client(workdir / "failures")
        if client is not None:
            svc = isolated_service(workdir / "llm", {})
            svc.deps.extractors["llm"] = LLMExtractor(client, svc.deps.materials)
            out["llm"] = run(svc, "llm")
            out["llm_stats"] = dict(client.stats)
    return out


def _table(rows: list[Row]) -> list[str]:
    out = [
        "| Sample | Expected tier | Actual | Confidence | Recommended plant | Unit price | Result | Notes |",
        "|---|---|---|---:|---|---:|---|---|",
    ]
    for r in rows:
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "–"
        out.append(
            f"| {r.rfq_id} | {r.expected.value} | {r.actual} | {conf} | {r.plant or '–'} | {r.unit_price} "
            f"| {'✅ pass' if r.passed else '❌ fail'} | {r.note} |"
        )
    return out


def render(res: dict) -> str:
    ref, llm = res["reference"], res["llm"]
    n_ref = sum(r.passed for r in ref)
    lines = [
        "# Routing (triage) check — 4 demo RFQs",
        "",
        "_Generated by `eval/eval_routing.py` (`make eval`). Each run uses a private copy of the database, "
        f'checkpoint store and quote directory; "today" is fixed at {TODAY.isoformat()} for the due-date '
        "checks. Prices are the recommended plant at the smallest offered quantity._",
        "",
        "Expected tiers from DESIGN §2: 0101 STANDARD (German e-mail, grinding, engineer edits a time), 0102 "
        "FAST_TRACK (reuse), 0103 MANUAL (missing material → clarification), 0104 STANDARD (assembly with an "
        "unpriced new part).",
        "",
        f"## (a) Reference extractor — {n_ref}/{len(ref)} pass",
        "",
        "Gold e-mail and drawing data; this isolates validation → similarity → routing → costing → "
        "confidence from extraction quality.",
        "",
        *_table(ref),
        "",
    ]
    if llm is None:
        lines += [
            "## (b) LLM extractor (replay) — skipped",
            "",
            "No recordings under `fixtures/llm_cache/`; run `eval/eval_extraction.py --mode record` with a "
            "model to create them.",
            "",
        ]
    else:
        n = sum(r.passed for r in llm)
        st = res["llm_stats"] or {}
        lines += [
            f"## (b) LLM extractor, replay mode — {n}/{len(llm)} pass",
            "",
            "E-mail and drawings read by the recorded local model (`fixtures/llm_cache/`, replayed exactly; "
            f"{st.get('cache_hits', 0)} cache hits, {st.get('llm_calls', 0)} live calls). Everything after "
            "extraction is identical to (a).",
            "",
            *_table(llm),
            "",
        ]
        diff = [
            (a, b)
            for a, b in zip(ref, llm, strict=True)
            if (a.actual, a.plant, a.unit_price) != (b.actual, b.plant, b.unit_price)
        ]
        if diff:
            lines += ["Differences between (a) and (b):", ""]
            for a, b in diff:
                lines.append(
                    f"- {a.rfq_id}: {a.actual} / {a.plant} / {a.unit_price} (reference) vs "
                    f"{b.actual} / {b.plant} / {b.unit_price} (LLM)" + (f" — {b.note}" if b.note else "")
                )
            lines.append("")
        else:
            lines += ["Tier, plant and price are identical to the reference run for all four samples.", ""]
    lines += [
        "## What each sample demonstrates",
        "",
        *[f"- **{k}** — {v}" for k, v in SHOWS.items()],
        "",
        "A pass means the tier matches the design. It does not mean the price is right — see `backtest.md` "
        "for cost accuracy and `extraction.md` for extraction accuracy.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-llm", action="store_true", help="reference extractor only")
    ap.add_argument("--out", type=Path, default=REPORT)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="rfq-eval-routing-") as tmp:
        res = evaluate(Path(tmp), llm=not args.no_llm)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(res), encoding="utf-8")
    for name in ("reference", "llm"):
        rows = res[name]
        if rows is None:
            print(f"{name:<9} skipped (no LLM recordings)")
            continue
        for r in rows:
            print(
                f"{name:<9} {r.rfq_id}  expected {r.expected.value:<10} actual {r.actual:<22} "
                f"{r.plant or '-':<3} {r.unit_price:<16} {'PASS' if r.passed else 'FAIL'}  {r.note}"
            )
    print(f"→ {args.out}")
    return 0 if all(r.passed for r in res["reference"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
