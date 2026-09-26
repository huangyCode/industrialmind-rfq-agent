"""Extraction eval (DESIGN §9): drawings + RFQ emails through the normal code path, scored against gold.

Two drawing sets:
- tuning  - the 4 RFQ samples + GR-3340 (eval_only). Prompts and repair rules were written while looking at
            these, so their scores are IN-SAMPLE.
- holdout - data/samples/holdout: 3 parts added after the prompts and repair rules were frozen; never tuned on.

Each drawing is scored twice from the same recorded model answers:
- model+repair: the production path (`extract_drawing`);
- model-only:   the same path with `repair_features` and `check_envelope` replaced by pass-throughs, i.e. the
                model's own feature list / envelope (structural merges - gear-data table -> teeth feature,
                assembly -> no features - and post-processing still apply).

    uv run python eval/eval_extraction.py --mode replay          # offline, from fixtures/llm_cache
    uv run python eval/eval_extraction.py --mode record --prune  # live model, rewrite cache, drop stale entries

Writes eval/reports/extraction.md (and prints the summary). Replay reproduces the recorded numbers exactly,
including latency and tokens, because both are stored with each cache entry.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from statistics import median
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rfq_agent.agents import drawing as drawing_agent  # noqa: E402
from rfq_agent.agents.drawing import extract_drawing  # noqa: E402
from rfq_agent.agents.intake import parse_rfq  # noqa: E402
from rfq_agent.config import LLM_CACHE_DIR, LLMSettings  # noqa: E402
from rfq_agent.llm.client import LLMClient, LLMError, get_client  # noqa: E402
from rfq_agent.models import DrawingSpec, RFQRequest  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
REPORT = ROOT / "eval" / "reports" / "extraction.md"
LLM_TASKS = ("intake", "drawing_title_block", "drawing_notes", "drawing_views")
TB_FIELDS = (
    "part_number",
    "revision",
    "title",
    "material",
    "general_tolerance",
    "default_ra_um",
    "scale",
    "units",
    "drawn_by",
    "date",
)
PL_FIELDS = ("part_number", "description", "quantity", "material", "standard")
NOMINAL_TOL_MM = 0.5


# ---------- normalisation ----------


def norm(v) -> str:
    if v is None:
        return ""
    s = str(v).lower().replace("×", "x").replace("ø", "").replace("⌀", "")
    s = re.sub(r"\s*([x:/+\-.,;])\s*", r"\1", s)
    return re.sub(r"\s+", " ", s).strip(" .")


def num_eq(a, b, rel: float = 0.01) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= rel * max(abs(float(b)), 1e-9)


def val_eq(a, b) -> bool:
    if isinstance(b, (int, float)) and not isinstance(b, bool) or isinstance(a, (int, float)):
        try:
            return num_eq(a, b)
        except (TypeError, ValueError):
            return False
    return norm(a) == norm(b)


STOP = {"with", "the", "and", "per", "of", "in", "as", "a", "an", "to", "for", "be", "by", "on"}


def tokens(s: str | None) -> set[str]:
    """Word stems (first 5 chars) so 'delivered' ~ 'delivery', 'assembled' ~ 'assembly'."""
    return {t[:5] for t in re.findall(r"[a-z0-9äöüß.]+", norm(s)) if t not in STOP}


def text_match(pred: str | None, gold: str | None, min_recall: float = 0.6) -> bool:
    """Free-text fields (treatments, requirements): gold tokens mostly present in the prediction."""
    if not gold or not pred:
        return not gold and not pred
    g = tokens(gold)
    return bool(g) and len(g & tokens(pred)) / len(g) >= min_recall


# ---------- drawing scoring ----------


def match_features(pred, gold) -> list[tuple]:
    """One-to-one greedy matching on type + nominal ±0.5 mm (closest nominal first)."""
    cands = []
    for i, g in enumerate(gold):
        for j, p in enumerate(pred):
            if p.type != g.type:
                continue
            if g.nominal_mm is None or p.nominal_mm is None:
                d = 0.0 if g.nominal_mm is None and p.nominal_mm is None else None
            else:
                d = abs(p.nominal_mm - g.nominal_mm)
            if d is not None and d <= NOMINAL_TOL_MM:
                cands.append((d, i, j))
    used_g, used_p, pairs = set(), set(), []
    for _d, i, j in sorted(cands):
        if i not in used_g and j not in used_p:
            used_g.add(i)
            used_p.add(j)
            pairs.append((gold[i], pred[j]))
    return pairs


def tol_eq(p, g) -> bool:
    if g is None:
        return p is None or (p.fit is None and p.upper is None and p.lower is None)
    if p is None:
        return False
    if g.fit:
        return (p.fit or "").replace(" ", "") == g.fit
    return num_eq(p.upper, g.upper, 0.001) and num_eq(p.lower, g.lower, 0.001)


def score_drawing(pred: DrawingSpec, gold: DrawingSpec) -> dict:
    r: dict = {"drawing": gold.drawing_file}
    tb_ok = {f: val_eq(getattr(pred.title_block, f), getattr(gold.title_block, f)) for f in TB_FIELDS}
    r["tb"] = tb_ok
    r["tb_miss"] = [
        f"{f}: got {getattr(pred.title_block, f)!r}, want {getattr(gold.title_block, f)!r}"
        for f, ok in tb_ok.items()
        if not ok
    ]
    e, ge = pred.envelope, gold.envelope
    env_fields = ["shape_class"] + [
        f for f in ("max_diameter_mm", "length_mm", "width_mm", "height_mm") if getattr(ge, f) is not None
    ]
    r["env"] = {f: val_eq(getattr(e, f), getattr(ge, f)) for f in env_fields}

    pairs = match_features(pred.features, gold.features)
    r["feat_tp"], r["feat_pred"], r["feat_gold"] = len(pairs), len(pred.features), len(gold.features)
    matched_gold = {id(g) for g, _ in pairs}
    matched_pred = {id(p) for _, p in pairs}
    r["feat_missed"] = [
        f"{g.type} {g.nominal_mm} ({g.evidence.text if g.evidence else ''})"
        for g in gold.features
        if id(g) not in matched_gold
    ]
    r["feat_extra"] = [
        f"{p.type} {p.nominal_mm} ({p.evidence.text if p.evidence else p.description})"
        for p in pred.features
        if id(p) not in matched_pred
    ]
    # tolerance / fit: over gold features that carry a tolerance; unmatched ones count as wrong
    by_gold = {id(g): p for g, p in pairs}
    tol_gold = [g for g in gold.features if g.tolerance is not None]
    r["tol_total"] = len(tol_gold)
    r["tol_ok"] = sum(
        1 for g in tol_gold if id(g) in by_gold and tol_eq(by_gold[id(g)].tolerance, g.tolerance)
    )
    r["tol_miss"] = [
        f"{g.evidence.text if g.evidence else g.nominal_mm}: got "
        + (repr(_tol_str(by_gold[id(g)].tolerance)) if id(g) in by_gold else "feature not found")
        for g in tol_gold
        if not (id(g) in by_gold and tol_eq(by_gold[id(g)].tolerance, g.tolerance))
    ]
    # tolerance present where gold has none (false fit codes) on matched pairs
    r["tol_false"] = sum(1 for g, p in pairs if g.tolerance is None and not tol_eq(p.tolerance, None))
    r["qty_ok"] = sum(1 for g, p in pairs if g.quantity == p.quantity)
    r["len_ok"] = sum(1 for g, p in pairs if num_eq(p.length_mm, g.length_mm))

    r["ht"] = text_match(pred.heat_treatment, gold.heat_treatment)
    r["st"] = text_match(pred.surface_treatment, gold.surface_treatment)
    r["ht_st_detail"] = (
        f"HT {pred.heat_treatment!r} / want {gold.heat_treatment!r}; "
        f"ST {pred.surface_treatment!r} / want {gold.surface_treatment!r}"
    )
    gn = [n for n in gold.notes]
    r["notes_ok"] = sum(1 for n in gn if any(text_match(p, n, 0.8) for p in pred.notes))
    r["notes_total"] = len(gn)

    if gold.parts_list:
        pl = {p.item_no: p for p in pred.parts_list}
        rows_ok, fields_ok, miss = 0, 0, []
        for g in gold.parts_list:
            p = pl.get(g.item_no)
            oks = {f: p is not None and val_eq(getattr(p, f), getattr(g, f)) for f in PL_FIELDS}
            fields_ok += sum(oks.values())
            rows_ok += all(oks.values())
            miss += [
                f"pos {g.item_no} {f}: got {getattr(p, f) if p else None!r}, want {getattr(g, f)!r}"
                for f, ok in oks.items()
                if not ok
            ]
        r["pl"] = {
            "rows_ok": rows_ok,
            "rows": len(gold.parts_list),
            "rows_pred": len(pred.parts_list),
            "fields_ok": fields_ok,
            "fields": len(gold.parts_list) * len(PL_FIELDS),
            "miss": miss,
        }
    return r


def _tol_str(t) -> str | None:
    if t is None:
        return None
    return t.fit or f"{t.upper}/{t.lower}"


# ---------- email scoring ----------


def score_email(pred: RFQRequest, gold: RFQRequest) -> dict:
    r: dict = {"rfq": gold.rfq_id, "fields": {}, "miss": []}

    def add(name, ok, got, want):
        r["fields"][name] = ok
        if not ok:
            r["miss"].append(f"{name}: got {got!r}, want {want!r}")

    for f in (
        "customer_name",
        "contact_name",
        "contact_email",
        "language",
        "received_at",
        "incoterm",
        "currency",
    ):
        add(f, val_eq(getattr(pred, f), getattr(gold, f)), getattr(pred, f), getattr(gold, f))
    add("item_count", len(pred.items) == len(gold.items), len(pred.items), len(gold.items))
    for g, p in zip(gold.items, pred.items, strict=False):
        n = g.line_no
        add(f"item{n}.quantities", p.quantities == g.quantities, p.quantities, g.quantities)
        add(
            f"item{n}.requested_delivery",
            p.requested_delivery == g.requested_delivery,
            str(p.requested_delivery),
            str(g.requested_delivery),
        )
        add(f"item{n}.drawing_ref", p.drawing_ref == g.drawing_ref, p.drawing_ref, g.drawing_ref)
        add(
            f"item{n}.customer_part_number",
            val_eq(p.customer_part_number, g.customer_part_number),
            p.customer_part_number,
            g.customer_part_number,
        )
    sr_hit = [
        g for g in gold.special_requirements if any(text_match(p, g) for p in pred.special_requirements)
    ]
    sr_prec = [
        p for p in pred.special_requirements if any(text_match(p, g) for g in gold.special_requirements)
    ]
    r["sr"] = (len(sr_hit), len(gold.special_requirements), len(sr_prec), len(pred.special_requirements))
    for g in gold.special_requirements:
        if g not in sr_hit:
            r["miss"].append(f"special requirement missed: {g!r} (got {pred.special_requirements})")
    for p in pred.special_requirements:
        if p not in sr_prec:
            r["miss"].append(f"special requirement extra: {p!r}")
    return r


# ---------- run ----------


def material_repo():
    from gen_master_data import load_master

    from rfq_agent.data.db import init_db
    from rfq_agent.data.repositories import MaterialRepo

    conn = init_db(Path(tempfile.mkdtemp()) / "eval.db")
    load_master(conn)
    return MaterialRepo(conn)


SETS = ("tuning", "holdout")


def drawing_cases() -> list[tuple[Path, Path, str | None, str]]:
    """(pdf, gold json, rfq id, set) for the tuning set and the hold-out set."""
    out = []
    for d in sorted(SAMPLES.glob("RFQ-*")) + [SAMPLES / "eval_only", SAMPLES / "holdout"]:
        for pdf in sorted((d / "drawings").glob("*.pdf")):
            out.append(
                (
                    pdf,
                    d / "expected" / f"{pdf.stem}.json",
                    d.name if d.name.startswith("RFQ") else None,
                    "holdout" if d.name == "holdout" else "tuning",
                )
            )
    return out


def _no_repair(features, envelope=None):
    return [f.model_copy(deep=True) for f in features], []


def _no_envelope_check(env, scale):
    return env.model_copy(), []


@contextmanager
def model_only():
    """Run `extract_drawing` without the deterministic feature / envelope repairs (score raw model output)."""
    with (
        mock.patch.object(drawing_agent, "repair_features", _no_repair),
        mock.patch.object(drawing_agent, "check_envelope", _no_envelope_check),
    ):
        yield


def extract_model_only(pdf: Path, client: LLMClient, materials, rfq_id: str | None = None) -> DrawingSpec:
    with model_only():
        spec, _ = extract_drawing(pdf, client, materials, rfq_id=rfq_id)
    return spec


def run(client: LLMClient, raw_client: LLMClient | None = None) -> dict:
    """`raw_client` (replay, same cache) serves the model-only pass so it neither re-calls the model nor
    shows up in the trace used for latency / pruning; defaults to `client`."""
    materials = material_repo()
    res: dict = {"drawings": [], "emails": [], "errors": []}
    for pdf, gold_path, rfq_id, dset in drawing_cases():
        gold = DrawingSpec.model_validate_json(gold_path.read_text(encoding="utf-8"))
        n0 = len(client.trace)
        try:
            spec, _ = extract_drawing(pdf, client, materials, rfq_id=rfq_id)
            raw = extract_model_only(pdf, raw_client or client, materials, rfq_id=rfq_id)
        except LLMError as e:
            res["errors"].append(f"{pdf.name}: {type(e).__name__}: {e}")
            continue
        s = score_drawing(spec, gold)
        s["set"] = dset
        s["raw"] = score_drawing(raw, gold)
        s["calls"] = client.trace[n0:]
        s["warnings"] = spec.extraction_warnings
        res["drawings"].append(s)
    for d in sorted(SAMPLES.glob("RFQ-*")):
        gold = RFQRequest.model_validate_json((d / "expected" / "rfq.json").read_text(encoding="utf-8"))
        n0 = len(client.trace)
        try:
            req = parse_rfq(d, client)
        except LLMError as e:
            res["errors"].append(f"{d.name} email: {type(e).__name__}: {e}")
            continue
        s = score_email(req, gold)
        s["calls"] = client.trace[n0:]
        res["emails"].append(s)
    return res


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}% ({a}/{b})" if b else "n/a"


def summarize_drawings(D: list[dict]) -> dict:
    """Pooled drawing metrics over a list of per-drawing score dicts."""
    tb = [ok for d in D for ok in d["tb"].values()]
    env = [ok for d in D for ok in d["env"].values()]
    return {
        "n": len(D),
        "tb": (sum(tb), len(tb)),
        "env": (sum(env), len(env)),
        "feat_p": (sum(d["feat_tp"] for d in D), sum(d["feat_pred"] for d in D)),
        "feat_r": (sum(d["feat_tp"] for d in D), sum(d["feat_gold"] for d in D)),
        "tol": (sum(d["tol_ok"] for d in D), sum(d["tol_total"] for d in D)),
        "tol_false": sum(d["tol_false"] for d in D),
        "treat": (sum(d["ht"] + d["st"] for d in D), 2 * len(D)),
        "notes": (sum(d["notes_ok"] for d in D), sum(d["notes_total"] for d in D)),
    }


def by_set(res: dict) -> dict[str, dict[str, dict]]:
    """{set: {"raw": metrics, "repair": metrics}}"""
    out = {}
    for name in SETS:
        D = [d for d in res["drawings"] if d.get("set", "tuning") == name]
        if D:
            out[name] = {"raw": summarize_drawings([d["raw"] for d in D]), "repair": summarize_drawings(D)}
    return out


def summarize(res: dict) -> dict:
    D = [d for d in res["drawings"] if d.get("set", "tuning") == "tuning"]
    tb = [ok for d in D for ok in d["tb"].values()]
    env = [ok for d in D for ok in d["env"].values()]
    tp = sum(d["feat_tp"] for d in D)
    npred = sum(d["feat_pred"] for d in D)
    ngold = sum(d["feat_gold"] for d in D)
    ef = [ok for e in res["emails"] for ok in e["fields"].values()]
    return {
        "tb": (sum(tb), len(tb)),
        "env": (sum(env), len(env)),
        "feat_p": (tp, npred),
        "feat_r": (tp, ngold),
        "tol": (sum(d["tol_ok"] for d in D), sum(d["tol_total"] for d in D)),
        "tol_false": sum(d["tol_false"] for d in D),
        "treat": (sum(d["ht"] + d["st"] for d in D), 2 * len(D)),
        "notes": (sum(d["notes_ok"] for d in D), sum(d["notes_total"] for d in D)),
        "email": (sum(ef), len(ef)),
        "sr_r": (sum(e["sr"][0] for e in res["emails"]), sum(e["sr"][1] for e in res["emails"])),
        "sr_p": (sum(e["sr"][2] for e in res["emails"]), sum(e["sr"][3] for e in res["emails"])),
        "errors": len(res["errors"]),
    }


HEADLINE = """
**Read this first.** The *tuning set* (4 RFQ drawings + GR-3340) is **in-sample**: the prompts and the
deterministic repair rules (`repair_features`, `check_envelope` in `agents/drawing.py`) were written while looking
at the model's errors on exactly these 5 sheets, so its numbers show that the pipeline can read them, not how well
it generalises. The *hold-out set* (3 parts, `data/samples/holdout`) was generated only after prompts and repair
rules were frozen (md5 below) and nothing was changed after seeing its results. It is still tiny (3 sheets,
FEATURES_HOLDOUT gold features) and synthetic (clean vector PDFs from the same generator), so treat it as a sanity
check of generalisation, not as a production accuracy estimate.
"""

HOLDOUT_ANALYSIS = """
## Hold-out error analysis

Written once after the single hold-out recording; no prompt or repair rule was changed afterwards.

**Summary.** On the 3 unseen parts the model alone finds 19 of 22 features (86 %), reads the title block in a new
field order / label set 100 %, and gets envelope, treatments, notes and the tooth-data table right. The repairs
add nothing on the hold-out: they fix one error and introduce one (below). On the tuning set the same frozen
prompts give 71 % model-only and 96 % with repairs - i.e. the repair rules mostly encode the tuning set's own
failure modes (shaft Ø with lower-case fit labelled `bore`, largest Ø only in the envelope), which the unseen
parts happen not to trigger.

The 3 misses:

1. **PS-2045, spline "DIN 5480-W25×1.25×18×8f" -> `groove`, nominal 1.25, fit "8f".** Unfamiliar notation: the
   prompt lists the `spline` type but shows no spline designation, and the model took the module (1.25) as the
   size. Pure model error; would drop the spline-hobbing operation. No warning is raised.
2. **PS-2045, centre holes "2× DIN 332-A3.15/6.7" -> `groove` (nominal 3.15 right, "6.7" as a fit code).** Partly a
   convention gap: the prompt defines `hole` as drilled *off* the axis and `bore` as turned *on* the axis, so a
   centre hole fits neither; gold calls it `hole`. Low cost impact (centre drilling is part of the turning
   set-up), but the "6.7" becomes a spurious tolerance.
3. **CP-7390, centre register bore "Ø56 H8": the model said `bore` (correct) and `repair_features` turned it into
   `hole`.** The rule "every bore on a prismatic part is a hole" was written for BR-0930's drilled holes and
   over-generalises to large bored seats - which the prompt itself calls `bore`. The same rule fixed the
   counterbore "C'BORE Ø20 depth 13" (model: `bore`), so recall is unchanged but tolerance accuracy drops from
   9/9 (model-only) to 8/9. This is the clearest sign that a repair rule was fitted to the tuning set.

Wrong but not counted in recall: counterbore quantity 1 instead of 4 (the "4×" is on the line above in the same
callout); thread classes "6H" / "6g" put into `tolerance.fit` (2 spurious tolerances - a defensible reading, the
schema has no thread-class field); lengths / depths are filled for only about half of the matched features on
both sets (not scored, but they feed cycle times).

**Effect of the prompt hygiene on the tuning set.** Replacing the sample-valued examples lowered in-sample
model-only recall from 83 % (previous report) to 71 %, and GR-3340's hub keyway "8 JS9" is now read as "8 J59"
and listed as a bore - the old base prompt contained the literal string "8 JS9". Part of the earlier 100 % was
the prompt quoting the test answers. The +repair tuning score is now 96 % (23/24) instead of 100 %.

**Honest expectation.** On clean, vector-rendered sheets of unseen parts: title block ~100 %, feature recall in
the mid-80s, with systematic weak spots on notations the prompt never shows (splines, centre holes, thread
classes) and on quantities split across callout lines. Scanned or hand-annotated drawings are not measured here.
None of the three hold-out sheets produced a single extraction warning, so the confidence score does not see
these errors; see the next section for what does.
"""


SAFETY_NET = """
## What happens downstream with such errors

Extraction output is not priced blindly (`agents/confidence.py`, `agents/review_rules.py`):

- **Low confidence.** The extraction score is completeness (part number, material, envelope, features,
  quantity) × share of fields / features that carry evidence text × (1 − 0.1 per extraction warning). Repair and
  post-processing warnings ("Largest outer diameter added from the envelope", "Envelope … cannot fit on the A3
  sheet", "Envelope incomplete", "Ø.. exceeds envelope", "thread without a thread designation", unknown
  material) each lower it. Features that no routing rule covers lower the coverage score.
- **Validation rules.** Empty material (VAL-001) and missing quantity are BLOCKERS -> MANUAL; unmapped material
  (VAL-002), no general tolerance (VAL-004) and the DFM rules are warnings, and any warning prevents FAST_TRACK.
- **Engineer review.** FAST_TRACK additionally needs a close historical precedent (similarity ≥ 0.85) and a
  price confirmed by a reference part. A new part family - which is exactly what the hold-out parts are - has no
  close precedent, so it lands in STANDARD or MANUAL, where an engineer checks the extracted spec against the
  drawing before the quote is released.

The three hold-out parts score 0.59 / 0.74 / 0.61 against the closest historical part (gold specs, same
similarity code), so none of them could be fast-tracked whatever the extraction said.

What the net does *not* catch by itself: a feature the model simply did not list, or listed with the wrong type,
raises no warning (the hold-out sheets had none). The routing then misses (or mis-chooses) an operation and the
cost is too low unless the reviewer notices it. For novel parts the review step, not the extractor, is the
control; a cheap next step would be a text-layer cross-check (every Ø / M callout in the PDF text layer should
map to a feature) that turns silent misses into warnings.
"""


HISTORY = """
## Earlier iteration log (tuning set only, prompts before the hygiene pass)

These are the steps of the previous evaluation cycle, with the earlier prompt versions (base v2 / views v3 etc.).
The current prompts differ only in their example values (see "Prompt hygiene" above).

| # | Change | Feature recall | Feature precision | Tolerance | Envelope |
|---|---|---|---|---|---|
| 0 | Baseline: views v2 | 75 % (18/24) | 72 % (18/25) | 70 % (7/10) | 100 % (16/16) |
| 1 | views **v3**: turned vs milled part rules, fit letter case, hub keyway + keyway depth, pocket width example, largest Ø is a feature, `face` only when toleranced, two-letter tolerance classes | 83 % (20/24) | 87 % (20/23) | 70 % (7/10) | 94 % (15/16) |
| 2 | `repair_features`: enforce fit case, key-slot class -> keyway, prismatic bore -> hole, pocket width/length order, drop untoleranced faces, add largest Ø from the envelope | 100 % (24/24) | 100 % (24/24) | 100 % (10/10) | 94 % (15/16) |
| 3 | `check_envelope`: null overall sizes that cannot fit the sheet at the printed scale (+ warning) | 100 % | 100 % | 100 % | 94 % (misread now surfaced, not passed on) |

## Model / runtime notes

- Ollama honours the JSON schema in `format`; every key is marked required (`providers.require_all`) so the model
  decides each field explicitly.
- gemma4 thinks by default; `think: false` gives the same answer ~5× faster on a title-block crop, so thinking
  stays off. Temperature 0, seed 0.
- Each image costs ~280 prompt tokens regardless of pixel size (fixed visual-token budget), hence the tiling:
  overview + up to 4 content tiles for the views, separate title-block and right-column crops.
"""

PROMPT_HYGIENE = """
## Prompt hygiene and freeze

Before the hold-out run, every example value in the active prompts that also appeared on a sample drawing or in a
sample email ("Ø35 k6", "42CrMo4+QT", "M12x1.75", "4x Ø9 THRU", "25 ±0.1", "QT 28-32 HRC", "anodized black",
"First article inspection report", ...) was replaced by a neutral value that appears on no tuning or hold-out
sheet ("Ø22 g6", "C45+N", "M10x1.5", "6x Ø11 THRU", "18 ±0.2", ...): base v3, title block v2, notes v3,
views v4, intake v3. No rule text was changed. The tuning-set numbers below are with these prompts (re-recorded).
Not cleaned (outside the prompt files): the field descriptions of the Pydantic output schemas, which are sent to
the model as part of the JSON schema, still use a few sample-like examples ('k6', 'H7', 'M12x1.75',
'ISO 2768-mK', 'QT 28-32 HRC', 'anodized black', 'DIN 625'). None of them is a hold-out value except the
generic fit code H7. The rules themselves (e.g. hub keyway depth, pocket L×W order) were also written from
tuning-set errors - that is what the hold-out is for.
Frozen before the hold-out was generated (md5):

FREEZE_MD5
"""

# md5 recorded at the freeze (2026-09-26), before the hold-out drawings existed; the report also says whether
# the files still match
FREEZE_MD5 = {
    "src/rfq_agent/llm/prompts/drawing_extract.v3.md": "d39961621f696cdfc9eee25e6fa1b570",
    "src/rfq_agent/llm/prompts/drawing_title_block.v2.md": "118b03e91e0e139c16d759380171d51b",
    "src/rfq_agent/llm/prompts/drawing_notes.v3.md": "b9b18adef024d7f0ce12cbd505075f93",
    "src/rfq_agent/llm/prompts/drawing_views.v4.md": "4a3c082b12c7363a8d32d8bdbe4ee7b9",
    "src/rfq_agent/llm/prompts/intake.v3.md": "e029b19fcbba6885c0b89352ab09ff25",
    "src/rfq_agent/agents/drawing.py": "7205c219475b9f06651b6bc7b635c2f0",
}


def _md5(path: Path) -> str:
    import hashlib

    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else "missing"


def _issues(d: dict) -> list[str]:
    return (
        [f"title block — {m}" for m in d["tb_miss"]]
        + [f"missed feature — {m}" for m in d["feat_missed"]]
        + [f"extra feature — {m}" for m in d["feat_extra"]]
        + [f"tolerance — {m}" for m in d["tol_miss"]]
        + [f"envelope — {f} wrong" for f, ok in d["env"].items() if not ok]
        + ([f"treatment — {d['ht_st_detail']}"] if not (d["ht"] and d["st"]) else [])
        + (d["pl"]["miss"] if "pl" in d else [])
    )


def report(res: dict, mode: str) -> str:
    S = summarize(res)
    B = by_set(res)
    calls = [c for d in res["drawings"] for c in d["calls"]] + [c for e in res["emails"] for c in e["calls"]]
    models = sorted({f"{c['provider']}/{c['model']}" for c in calls})
    rec_dates = sorted({(c.get("recorded_at") or "")[:10] for c in calls if c.get("recorded_at")})
    versions = sorted({f"{c['node']}.{c['prompt_version']}" for c in calls})
    L: list[str] = []
    w = L.append
    w("# Extraction evaluation\n")
    w(f"- Model: {', '.join(models) or 'n/a'}; prompts: {', '.join(versions)}")
    w(f"- Recorded: {', '.join(rec_dates) or 'n/a'}; this report generated {date.today()} in `{mode}` mode")
    w(
        "- Scoring: strings normalised (case, whitespace, ×/x, Ø); numbers within 1 %. Features matched one-to-one"
    )
    w(
        "  on type + nominal ±0.5 mm. Tolerance accuracy is over gold features that carry a fit code or deviations"
    )
    w("  (a missed feature counts as a wrong tolerance). Free text (treatments, notes, special requirements)")
    w("  counts as matched when ≥60 % (notes: 80 %) of the gold words appear in the prediction.")
    w(
        "- *Model-only* = the model's own answer (no `repair_features` / `check_envelope`); *+repair* = production path.\n"
    )

    w("## Headline: tuning (in-sample) vs hold-out\n")
    w(
        "| Set | Drawings | Title block | Feature recall: model-only | +repair | "
        "Feature precision: model-only | +repair | Tolerance acc.: model-only | +repair |"
    )
    w("|---|---|---|---|---|---|---|---|---|")
    for name, label in (("tuning", "Tuning (in-sample)"), ("holdout", "**Hold-out**")):
        if name not in B:
            continue
        r, f = B[name]["raw"], B[name]["repair"]
        w(
            f"| {label} | {f['n']} | {pct(*f['tb'])} | {pct(*r['feat_r'])} | {pct(*f['feat_r'])} | "
            f"{pct(*r['feat_p'])} | {pct(*f['feat_p'])} | {pct(*r['tol'])} | {pct(*f['tol'])} |"
        )
    w("")
    w("Title block is read by its own call and is not touched by the repairs (model-only = +repair).\n")
    nh = B.get("holdout", {}).get("repair", {}).get("feat_r", (0, 0))[1]
    w(HEADLINE.strip().replace("FEATURES_HOLDOUT", str(nh)))
    w("")
    w("### Other drawing metrics by set (+repair; model-only in brackets)\n")
    w("| Set | Envelope | Heat / surface treatment | Notes captured | Spurious tolerances |")
    w("|---|---|---|---|---|")
    for name in B:
        r, f = B[name]["raw"], B[name]["repair"]
        w(
            f"| {name} | {pct(*f['env'])} ({pct(*r['env'])}) | {pct(*f['treat'])} | {pct(*f['notes'])} | "
            f"{f['tol_false']} ({r['tol_false']}) |"
        )
    w("")
    freeze = "\n".join(
        f"    {h}  {p}" + ("" if _md5(ROOT / p) == h else "   <- CHANGED since the freeze")
        for p, h in FREEZE_MD5.items()
    )
    w(PROMPT_HYGIENE.strip().replace("FREEZE_MD5", freeze))
    w("")

    w("## Tuning-set details (in-sample)\n")
    w("| Metric | Result | Target |")
    w("|---|---|---|")
    w(f"| Title-block field accuracy | {pct(*S['tb'])} | ≥ 90 % |")
    w(f"| Feature recall (type + nominal) | {pct(*S['feat_r'])} | ≥ 80 % |")
    w(f"| Feature precision | {pct(*S['feat_p'])} | — |")
    w(f"| Tolerance / fit accuracy | {pct(*S['tol'])} | — |")
    w(f"| Spurious tolerances on matched features | {S['tol_false']} | — |")
    w(f"| Heat / surface treatment | {pct(*S['treat'])} | — |")
    w(f"| Envelope (shape class + overall dims) | {pct(*S['env'])} | — |")
    w(f"| Notes captured | {pct(*S['notes'])} | — |")
    pl = [d["pl"] for d in res["drawings"] if "pl" in d]
    if pl:
        p = pl[0]
        w(f"| Parts-list rows fully correct (ASM-5100) | {pct(p['rows_ok'], p['rows'])} | — |")
        w(f"| Parts-list fields correct (ASM-5100) | {pct(p['fields_ok'], p['fields'])} | — |")
    w(f"| Email field accuracy | {pct(*S['email'])} | — |")
    w(f"| Special requirements recall / precision | {pct(*S['sr_r'])} / {pct(*S['sr_p'])} | — |")
    w(f"| Extraction errors | {S['errors']} | 0 |")
    w("")
    if res["errors"]:
        w("Errors:\n")
        for e in res["errors"]:
            w(f"- {e}")
        w("")

    w("## Per drawing\n")
    w(
        "| Set | Drawing | Title block | Recall model-only | Recall +repair | Precision +repair | "
        "Tolerance +repair | HT | ST | Envelope |"
    )
    w("|---|---|---|---|---|---|---|---|---|---|")
    for d in res["drawings"]:
        env, r = d["env"], d["raw"]
        w(
            f"| {d['set']} | {d['drawing']} | {pct(sum(d['tb'].values()), len(d['tb']))} | "
            f"{pct(r['feat_tp'], r['feat_gold'])} | {pct(d['feat_tp'], d['feat_gold'])} | "
            f"{pct(d['feat_tp'], d['feat_pred'])} | {pct(d['tol_ok'], d['tol_total'])} | "
            f"{'ok' if d['ht'] else 'wrong'} | {'ok' if d['st'] else 'wrong'} | {pct(sum(env.values()), len(env))} |"
        )
    w("")
    for d in res["drawings"]:
        fixed = [i for i in _issues(d["raw"]) if i not in _issues(d)]
        issues = _issues(d)
        if not (issues or fixed):
            continue
        w(f"**{d['drawing']}** ({d['set']})\n")
        for i in issues:
            w(f"- {i}")
        for i in fixed:
            w(f"- model-only, fixed by repair: {i}")
        w("")

    w(HOLDOUT_ANALYSIS.strip())
    w("")
    w(SAFETY_NET.strip())
    w("")

    w("## Per email (tuning set)\n")
    w("| RFQ | Fields | Special requirements (recall) | Misses |")
    w("|---|---|---|---|")
    for e in res["emails"]:
        f = e["fields"]
        w(
            f"| {e['rfq']} | {pct(sum(f.values()), len(f))} | {pct(e['sr'][0], e['sr'][1])} | "
            f"{'; '.join(e['miss']) or '—'} |"
        )
    w("")

    w("## Latency and tokens per call\n")
    w(
        "Latency is wall time measured when the call was recorded (local Ollama on a 36 GB Apple-silicon laptop,"
    )
    w("model already loaded); replay serves the same numbers from the cache.\n")
    w("| Task | Calls | Median latency s | Max latency s | Median input tok | Median output tok | Retries |")
    w("|---|---|---|---|---|---|---|")
    by = defaultdict(list)
    for c in calls:
        by[c["node"]].append(c)
    for task in LLM_TASKS:
        cs = by.get(task, [])
        if not cs:
            continue
        lat = [c["latency_ms"] / 1000 for c in cs if c.get("latency_ms") is not None]
        w(
            f"| {task} | {len(cs)} | {median(lat):.1f} | {max(lat):.1f} | "
            f"{median(c.get('input_tokens', 0) for c in cs):.0f} | "
            f"{median(c.get('output_tokens', 0) for c in cs):.0f} | "
            f"{sum(max((c.get('attempts') or 1) - 1, 0) for c in cs)} |"
        )
    per_drawing = [sum((c.get("latency_ms") or 0) for c in d["calls"]) / 1000 for d in res["drawings"]]
    if per_drawing:
        w(
            f"\nPer drawing (3 calls, sequential): median {median(per_drawing):.1f} s, max {max(per_drawing):.1f} s."
        )
    w("")
    w(HISTORY.strip())
    return "\n".join(L) + "\n"


def prune_cache(client: LLMClient, cache_root: Path) -> list[Path]:
    """Delete cache entries of the extraction tasks that this run did not use (stale prompt versions)."""
    used = {Path(c["cache_path"]).resolve() for c in client.trace if c.get("cache_path")}
    removed = []
    for task in LLM_TASKS:
        for p in (cache_root / task).glob("*.json"):
            if p.resolve() not in used:
                p.unlink()
                removed.append(p)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("replay", "record", "live", "auto"), default="replay")
    ap.add_argument("--prune", action="store_true", help="after the run, delete unused cache entries")
    ap.add_argument("--out", type=Path, default=REPORT)
    ap.add_argument("--json", type=Path, help="also dump raw scores as JSON")
    a = ap.parse_args(argv)

    settings = dataclasses.replace(LLMSettings.from_env(), mode=a.mode)
    client = get_client(settings, call_repo=None)
    # model-only pass: replay the answers the main pass just used / recorded (no second model call)
    raw_client = get_client(dataclasses.replace(settings, mode="replay"), call_repo=None)
    res = run(client, raw_client)
    text = report(res, a.mode)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text, encoding="utf-8")
    if a.json:
        a.json.write_text(json.dumps(res, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(text)
    if a.prune:
        if res["errors"]:
            print("Not pruning: the run had errors.")
        else:
            removed = prune_cache(client, Path(settings.cache_dir) if settings.cache_dir else LLM_CACHE_DIR)
            print(f"Pruned {len(removed)} stale cache entries.")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
