"""Leave-one-out cost backtest (DESIGN §9, §1.4): rule engine alone vs rules + similar-part calibration.

For every historical part: take its stored DrawingSpec (`parts.spec_json`, i.e. the drawing read perfectly —
no VLM in the loop), remove the part itself from the similarity search, plan the routing, build the BOM and
run the costing engine for every (plant, qty) combination that has an actual cost in `part_costs`. The
estimated unit cost is compared with that actual.

    uv run python eval/backtest_costing.py            # all 90 parts → eval/reports/backtest.md + PNG
    uv run python eval/backtest_costing.py --limit 12  # quick look, no report written unless --out

Methods:
    rules       rule routing only (routing_rules.rule_routing)
    calibrated  rules + blending with the top similar part's MES times (what the pipeline quotes)
    accepted    calibrated + every suggested op accepted — a sensitivity row for "the engineer accepts the
                suggestions", not an automatic method
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rfq_agent.agents.bom import build_bom  # noqa: E402
from rfq_agent.agents.costing import compute_costs  # noqa: E402
from rfq_agent.agents.geometry import finished_volume_cm3, weight_kg  # noqa: E402
from rfq_agent.agents.postprocess import postprocess  # noqa: E402
from rfq_agent.agents.routing_rules import plan_routing  # noqa: E402
from rfq_agent.agents.similarity import find_similar, reuse_hint  # noqa: E402
from rfq_agent.config import BIZ, DB_PATH  # noqa: E402
from rfq_agent.data.repositories import (  # noqa: E402
    CatalogRepo,
    MaterialRepo,
    PartRepo,
    RateRepo,
    ServiceRepo,
)
from rfq_agent.models import DrawingSpec  # noqa: E402

REPORT_DIR = ROOT / "eval" / "reports"
COST_DATE = date(2026, 6, 30)  # same "today" the history costs were computed with
METHODS = ("rules", "calibrated", "accepted")
LABEL = {
    "rules": "Rules only",
    "calibrated": "Rules + similar-part calibration",
    "accepted": "Calibrated + suggested ops accepted",
}
TARGET_MDAPE, TARGET_W20 = 0.15, 0.75  # DESIGN §1.4
FAMILY_ORDER = ("shaft", "flange", "bracket", "housing", "gear", "assembly")


@dataclass
class Point:
    part_number: str
    family: str
    plant: str
    qty: int
    method: str
    actual: float
    estimate: float | None  # None = estimate infeasible at that plant

    @property
    def err(self) -> float:
        return (self.estimate - self.actual) / self.actual


def open_db(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Read-only connection: the backtest never writes to the database."""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def run_backtest(
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    part_numbers: list[str] | None = None,
) -> dict:
    """Leave-one-out over the history. Returns {"points": [Point], "parts": [per-part dict]}."""
    conn = conn or open_db()
    mats, parts = MaterialRepo(conn), PartRepo(conn)
    rates, services, catalog = RateRepo(conn), ServiceRepo(conn).all, CatalogRepo(conn)
    rows = parts.all()
    if part_numbers:
        rows = [r for r in rows if r["part_number"] in set(part_numbers)]
    if limit:
        rows = rows[:limit]

    points: list[Point] = []
    per_part: list[dict] = []
    for row in rows:
        raw = parts.spec_json(row["part_id"])
        actuals = {(c["plant"], c["qty"]): c["unit_cost_eur"] for c in parts.costs(row["part_id"])}
        if not raw or not actuals:
            continue
        spec = postprocess(DrawingSpec.model_validate_json(raw), mats)
        mat = mats.all.get(spec.title_block.material_code or "")
        similar = find_similar(spec, parts, mats, exclude_part_id=row["part_id"])
        fin = weight_kg(finished_volume_cm3(spec), mat.density_g_cm3) if mat else 0.0
        qtys = sorted({q for _, q in actuals})

        calibrated, _ = plan_routing(spec, mat, services, similar, parts)
        routings = {
            "rules": plan_routing(spec, mat, services, [], parts)[0],
            "calibrated": calibrated,
            "accepted": [op.model_copy(update={"suggested": False}) for op in calibrated],
        }
        for method, routing in routings.items():
            bom = build_bom(spec, mat, routing, catalog, parts)
            costs = compute_costs(
                routing=routing,
                bom=bom,
                quantities=qtys,
                finished_weight_kg=fin,
                requested_delivery=None,
                today=COST_DATE,
                rates=rates,
                services=services,
            )
            est = {(c.plant, c.qty): c.unit_cost_eur for c in costs if c.feasible}
            for (plant, qty), act in sorted(actuals.items()):
                points.append(
                    Point(row["part_number"], row["family"], plant, qty, method, act, est.get((plant, qty)))
                )
        top = similar[0] if similar else None
        per_part.append(
            {
                "part_number": row["part_number"],
                "family": row["family"],
                "top_similar": top.part_number if top else None,
                "top_score": top.score if top else 0.0,
                "calibrated": bool(top and top.score >= BIZ.blend_min_score),
                "reuse": reuse_hint(spec, similar, 1) is not None,
                "suggested_ops": [op.op_code for op in calibrated if op.suggested],
            }
        )
    return {"points": points, "parts": per_part}


# ---------- metrics ----------


def metrics(points: list[Point]) -> dict:
    ok = [p for p in points if p.estimate is not None]
    if not ok:
        return {"n": 0, "infeasible": len(points)}
    ape = [abs(p.err) for p in ok]
    return {
        "n": len(ok),
        "infeasible": len(points) - len(ok),
        "mdape": statistics.median(ape),
        "w10": sum(a <= 0.10 for a in ape) / len(ape),
        "w20": sum(a <= 0.20 for a in ape) / len(ape),
        "bias": statistics.median(p.err for p in ok),
        "mean_err": statistics.fmean(p.err for p in ok),
    }


def summarize(result: dict) -> dict:
    pts = result["points"]
    by_method = {m: [p for p in pts if p.method == m] for m in METHODS}
    fams = [f for f in FAMILY_ORDER if any(p.family == f for p in pts)]
    parts = result["parts"]
    return {
        "overall": {m: metrics(ps) for m, ps in by_method.items()},
        "de200": {
            m: metrics([p for p in ps if p.plant == "DE" and p.qty == 200]) for m, ps in by_method.items()
        },
        "family": {
            f: {m: metrics([p for p in ps if p.family == f]) for m, ps in by_method.items()} for f in fams
        },
        "plant": {
            pl: {m: metrics([p for p in ps if p.plant == pl]) for m, ps in by_method.items()}
            for pl in BIZ.plants
        },
        "n_parts": len(parts),
        "n_reuse": sum(p["reuse"] for p in parts),
        "reuse_parts": [p["part_number"] for p in parts if p["reuse"]],
        "n_calibrated": sum(p["calibrated"] for p in parts),
        "suggested": Counter(op for p in parts for op in p["suggested_ops"]),
        "suggested_by_family": {
            f: Counter(op for p in parts if p["family"] == f for op in p["suggested_ops"]) for f in fams
        },
    }


# ---------- report ----------


def _pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "–"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def plot(result: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # reference palette slots 1/2 (validated categorical pair), recessive ink for axes
    colors = {"rules": "#2a78d6", "calibrated": "#eb6834"}
    ink, muted, grid = "#0b0b0b", "#52514e", "#e4e3df"
    pts = [p for p in result["points"] if p.estimate is not None and p.method in colors]
    fams = [f for f in FAMILY_ORDER if any(p.family == f for p in pts)]

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": muted, "axes.labelcolor": ink})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.6), gridspec_kw={"width_ratios": [1, 1.5]})
    fig.patch.set_facecolor("#fcfcfb")
    bins = [x / 100 for x in range(-60, 65, 5)]
    for m, c in colors.items():
        errs = [max(-0.6, min(0.6, p.err)) for p in pts if p.method == m]
        ax1.hist(errs, bins=bins, histtype="step", linewidth=2, color=c, label=LABEL[m])
    for x in (-0.2, 0.2):
        ax1.axvline(x, color=muted, linewidth=1, linestyle=":")
    ax1.axvline(0, color=muted, linewidth=1)
    ax1.set_xlabel("Signed error (estimate − actual) / actual  [clipped at ±60 %]")
    ax1.set_ylabel("Part × plant × qty points")
    ax1.set_title("Error distribution, all points", loc="left", color=ink)
    ax1.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax1.legend(frameon=False, loc="upper left")

    data, pos, cols = [], [], []
    for i, f in enumerate(fams):
        for j, (m, c) in enumerate(colors.items()):
            data.append([p.err for p in pts if p.family == f and p.method == m])
            pos.append(i * 3 + j)
            cols.append(c)
    bp = ax2.boxplot(data, positions=pos, widths=0.75, patch_artist=True, showfliers=True)
    for patch, c in zip(bp["boxes"], cols, strict=True):
        patch.set(facecolor=c + "55", edgecolor=c, linewidth=1.5)
    for k in ("whiskers", "caps"):
        for i, line in enumerate(bp[k]):
            line.set(color=cols[i // 2], linewidth=1.2)
    for line, c in zip(bp["medians"], cols, strict=True):
        line.set(color=c, linewidth=2)
    for fl, c in zip(bp["fliers"], cols, strict=True):
        fl.set(marker="o", markersize=3, markerfacecolor=c, markeredgecolor=c, alpha=0.6)
    ax2.axhspan(-0.2, 0.2, color=grid, alpha=0.6, zorder=0)
    ax2.axhline(0, color=muted, linewidth=1)
    ax2.set_xticks([i * 3 + 0.5 for i in range(len(fams))], fams)
    ax2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax2.set_ylabel("Signed error")
    ax2.set_title("Signed error by family (band = ±20 %)", loc="left", color=ink)
    handles = [
        matplotlib.patches.Patch(facecolor=c + "55", edgecolor=c, label=LABEL[m]) for m, c in colors.items()
    ]
    ax2.legend(handles=handles, frameon=False, loc="upper left")
    for ax in (ax1, ax2):
        ax.set_facecolor("#fcfcfb")
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(colors=muted)
    fig.suptitle(
        "Leave-one-out cost backtest (synthetic history) — rules only vs rules + calibration",
        x=0.01,
        ha="left",
        color=ink,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)


def _metric_rows(title: str, ms: dict) -> list[str]:
    out = []
    for m in METHODS:
        x = ms[m]
        if not x.get("n"):
            continue
        out.append(
            f"| {title} | {LABEL[m]} | {x['n']} | {_pct(x['mdape'])} | {_pct(x['w10'])} | {_pct(x['w20'])} "
            f"| {_pct(x['bias'], True)} | {x['infeasible']} |"
        )
        title = ""
    return out


def fam_delta(s: dict, *, improved: bool) -> str:
    """'flange 14.2% → 6.5%, …' for families whose MdAPE improved by > 1 pt (or did not)."""
    out = []
    for f, ms in s["family"].items():
        r, c = ms["rules"].get("mdape"), ms["calibrated"].get("mdape")
        if r is None or c is None:
            continue
        if (r - c > 0.01) == improved:
            out.append(f"{f} {_pct(r)} → {_pct(c)}")
    label = "MdAPE rules → calibrated: " if improved else "No gain (MdAPE rules → calibrated): "
    return label + (", ".join(out) or "none")


def render(result: dict, s: dict, png_name: str) -> str:
    o, c, r = s["overall"]["calibrated"], s["overall"]["rules"], s["overall"]["accepted"]
    met = lambda x: "✅ met" if x else "❌ not met"  # noqa: E731
    hdr = "| Slice | Method | Points | MdAPE | within ±10 % | within ±20 % | Median bias | Infeasible |"
    sep = "|---|---|---:|---:|---:|---:|---:|---:|"
    fam_rows = []
    for f, ms in s["family"].items():
        fam_rows += _metric_rows(f, ms)
    plant_rows = []
    for pl, ms in s["plant"].items():
        plant_rows += _metric_rows(pl, ms)
    sugg = ", ".join(f"{k} ×{v}" for k, v in s["suggested"].most_common()) or "none"
    sugg_fam = "; ".join(
        f"{f}: " + ", ".join(f"{k} ×{v}" for k, v in cnt.most_common())
        for f, cnt in s["suggested_by_family"].items()
        if cnt
    )
    lines = [
        "# Cost backtest — leave-one-out over the synthetic history",
        "",
        "_Generated by `eval/backtest_costing.py` (`make eval`). All data is synthetic; see "
        "[How the data was generated](#how-the-data-was-generated) before reading any number._",
        "",
        "## Headline",
        "",
        f"{s['n_parts']} historical parts × their own (plant, qty) combinations = {o['n'] + o['infeasible']} "
        "points per method. Each part is removed from the similarity search before it is estimated; its "
        "drawing data is the stored `DrawingSpec` (perfect extraction — this isolates the cost engine from "
        "the VLM).",
        "",
        "| Metric | Rules only | Rules + calibration | DESIGN §1.4 target | Calibrated vs target |",
        "|---|---:|---:|---:|---|",
        f"| MdAPE | {_pct(c['mdape'])} | **{_pct(o['mdape'])}** | ≤ 15 % | {met(o['mdape'] <= TARGET_MDAPE)} |",
        f"| Share within ±20 % | {_pct(c['w20'])} | **{_pct(o['w20'])}** | ≥ 75 % | {met(o['w20'] >= TARGET_W20)} |",
        f"| Share within ±10 % | {_pct(c['w10'])} | **{_pct(o['w10'])}** | – | |",
        f"| Median bias (signed) | {_pct(c['bias'], True)} | **{_pct(o['bias'], True)}** | – | |",
        f"| Points not estimable (plant infeasible for the planned routing) | {c['infeasible']} | {o['infeasible']} | – | |",
        "",
        f"Sensitivity — if the engineer accepts every suggested operation: MdAPE {_pct(r['mdape'])}, "
        f"within ±20 % {_pct(r['w20'])}, bias {_pct(r['bias'], True)}.",
        "",
        f"Calibration was applied (top similar part score ≥ {BIZ.blend_min_score}) for "
        f"{s['n_calibrated']} of {s['n_parts']} parts. **REUSE-001 fired for {s['n_reuse']} of "
        f"{s['n_parts']} parts** under leave-one-out"
        + (f" ({', '.join(s['reuse_parts'])})" if s["reuse_parts"] else "")
        + ". Suggested (not costed) ops produced by calibration: "
        + sugg
        + ".",
        "",
        f"![Error distribution]({png_name})",
        "",
        "## By family",
        "",
        hdr,
        sep,
        *fam_rows,
        "",
        f"Suggested ops by family: {sugg_fam or 'none'}.",
        "",
        "## By plant",
        "",
        hdr,
        sep,
        *plant_rows,
        "",
        "## How the data was generated",
        "",
        "The history (90 parts: 25 shafts, 20 flanges/covers, 15 brackets, 10 housings, 12 gears, 8 "
        'assemblies) is produced by `scripts/gen_history.py` with a fixed seed. The "actual" times are **not** '
        "the rule engine plus noise — that would make the backtest grade its own homework. They come from an "
        "independent *hidden process model* (`hidden_routing`) with a different functional form and factors "
        "the rules cannot see:",
        "",
        "- material effect from a hardness table the rules never see, `(HB/200)^1.7 ×` chip factor "
        "(QT steels finish-turned at ~290 HB, stainless ×1.8), where the rules divide removed volume linearly "
        "by master-data machinability;",
        "- turning time driven by turned *surface* rather than removed volume; milling by clampings and "
        "pocket depths that are not on the drawing;",
        "- family-specific practice as a multiplier (flange 0.50 … housing 1.20) plus per-part lognormal "
        "noise σ = 0.12 and per-op noise σ = 0.05, a fixed changeover per batch, a small-batch learning "
        "penalty (×1.35 at 50 pcs … ×1.08 at 1,000) and crew factors (DE 1.00 / PL 1.06 / CN 1.12);",
        "- family-specific extra operations the rules never generate: STRAIGHTEN for long heat-treated "
        "shafts, a second MILL_FINISH setup after stress-relief ageing on cast housings, CMM inspection on "
        "every housing, TOOTH_CHAMFER on gears.",
        "",
        "The actual unit costs in `part_costs` are then computed with the same costing engine (rates, "
        "overhead, logistics, material, outsourcing) on those hidden times, so the backtest measures the "
        "*routing/time* estimate; rates and material prices are, by construction, identical.",
        "",
        "**Demo control.** A handful of anchor parts are placed by hand so the four demo RFQs behave as "
        "designed (e.g. FL-2150 near-identical to the demo flange, SH-4650 a moderately similar shaft). "
        "The generator also **re-draws any random variant that would out-rank an anchor against a demo "
        "drawing** (up to 30 attempts), so the demo's nearest neighbours are guaranteed. This shapes the "
        "neighbourhood of the four demo drawings only; it does not select parts by backtest error, but it "
        "is still a hand on the scale and is stated here plainly.",
        "",
        "## Reading the results",
        "",
        f"- **Where calibration helps.** {fam_delta(s, improved=True)}. Blending with the nearest part's MES "
        "times removes the error that is shared within a family — the hardness effect, surface-driven "
        "turning and family practice (e.g. flanges run at half the time the rules expect).",
        f"- **Where it does not.** {fam_delta(s, improved=False)}. The largest gap for housings and brackets is "
        "*operations the rules do not plan at all* (MILL_FINISH after stress-relief ageing, CMM instead of "
        "manual inspection). Calibration only blends times of ops that exist in both routings; ops that only "
        "the reference part has are surfaced as **suggestions** and are deliberately **not costed** until an "
        "engineer accepts them (a suggestion from a 0.6-similar part must not silently move a price). For "
        "housings calibration even makes it worse. Example HS-5101: the rules plan a SAW op (cast housings "
        "are not sawn) and manual inspection — both stay unblended because the reference has no matching "
        "op — while the reference's MILL time, which excludes its separate MILL_FINISH setup, pulls MILL "
        "down (10.2 → 8.2 min vs 10.3 min actual). The rules' accidental offset disappears and the bias "
        'moves further negative. The "suggested ops accepted" row shows the engineer closes most of the gap '
        "with one click per op — which is exactly why suggestions are listed in the review reasons.",
        "- **Residual bias is negative** (estimates too low): non-planned ops, the small-batch learning "
        "penalty and crew factors (PL ×1.06, CN ×1.12) are invisible to the rules. In production this is "
        "what the feedback loop recalibrates (see `feedback.md`).",
        "- **Per-part noise** (σ = 0.12 lognormal on every part, σ = 0.05 per op) is individual; no "
        "neighbour predicts it, so a floor of error remains for any method.",
        "- **Assemblies look almost perfect for a trivial reason:** their cost is dominated by purchased "
        "and internal part costs that are read from the same tables in estimate and actual; only assembly "
        "/ test time is estimated. Do not read the assembly row as evidence of accuracy.",
        "- **REUSE-001** fires for parts whose product line contains a near-duplicate variant (same "
        "material, main dimensions within 5 %). Under leave-one-out the part itself is excluded, so these "
        "are genuine near-twins, not self-matches.",
        "",
        "These numbers show that the chain (similarity → calibration → costing) works and that calibration "
        "adds value where the hidden process has learnable structure. They are **not** an accuracy claim "
        "for a real shop floor: in production the same script runs on ERP/MES history in shadow mode "
        "before any threshold is trusted.",
        "",
    ]
    return "\n".join(lines)


def write_report(result: dict, out_dir: Path = REPORT_DIR) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    s = summarize(result)
    png = out_dir / "backtest_errors.png"
    plot(result, png)
    (out_dir / "backtest.md").write_text(render(result, s, png.name), encoding="utf-8")
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="only the first N parts")
    ap.add_argument("--out", type=Path, default=None, help="report directory (default eval/reports)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()
    result = run_backtest(open_db(args.db), limit=args.limit)
    if args.limit and args.out is None:
        s = summarize(result)
    else:
        s = write_report(result, args.out or REPORT_DIR)
    for m in METHODS:
        x = s["overall"][m]
        print(
            f"{LABEL[m]:<38} n={x['n']:>4}  MdAPE {_pct(x['mdape']):>6}  ±10% {_pct(x['w10']):>6}  "
            f"±20% {_pct(x['w20']):>6}  bias {_pct(x['bias'], True):>7}  infeasible {x['infeasible']}"
        )
    print(f"REUSE-001 fired for {s['n_reuse']}/{s['n_parts']} parts; calibrated {s['n_calibrated']}")
    fam = defaultdict(dict)
    for f, ms in s["family"].items():
        for m in ("rules", "calibrated"):
            fam[f][m] = ms[m].get("mdape")
    for f, v in fam.items():
        print(f"  {f:<9} MdAPE rules {_pct(v['rules']):>6} → calibrated {_pct(v['calibrated']):>6}")


if __name__ == "__main__":
    main()
