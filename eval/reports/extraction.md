# Extraction evaluation

- Model: ollama/gemma4:12b; prompts: drawing_notes.v3+base.v3, drawing_title_block.v2+base.v3, drawing_views.v4+base.v3, intake.v3
- Recorded: 2026-09-26; this report generated 2026-09-26 in `replay` mode
- Scoring: strings normalised (case, whitespace, ×/x, Ø); numbers within 1 %. Features matched one-to-one
  on type + nominal ±0.5 mm. Tolerance accuracy is over gold features that carry a fit code or deviations
  (a missed feature counts as a wrong tolerance). Free text (treatments, notes, special requirements)
  counts as matched when ≥60 % (notes: 80 %) of the gold words appear in the prediction.
- *Model-only* = the model's own answer (no `repair_features` / `check_envelope`); *+repair* = production path.

## Headline: tuning (in-sample) vs hold-out

| Set | Drawings | Title block | Feature recall: model-only | +repair | Feature precision: model-only | +repair | Tolerance acc.: model-only | +repair |
|---|---|---|---|---|---|---|---|---|
| Tuning (in-sample) | 5 | 100% (50/50) | 71% (17/24) | 96% (23/24) | 77% (17/22) | 96% (23/24) | 60% (6/10) | 90% (9/10) |
| **Hold-out** | 3 | 100% (30/30) | 86% (19/22) | 86% (19/22) | 86% (19/22) | 86% (19/22) | 100% (9/9) | 89% (8/9) |

Title block is read by its own call and is not touched by the repairs (model-only = +repair).

**Read this first.** The *tuning set* (4 RFQ drawings + GR-3340) is **in-sample**: the prompts and the
deterministic repair rules (`repair_features`, `check_envelope` in `agents/drawing.py`) were written while looking
at the model's errors on exactly these 5 sheets, so its numbers show that the pipeline can read them, not how well
it generalises. The *hold-out set* (3 parts, `data/samples/holdout`) was generated only after prompts and repair
rules were frozen (md5 below) and nothing was changed after seeing its results. It is still tiny (3 sheets,
22 gold features) and synthetic (clean vector PDFs from the same generator), so treat it as a sanity
check of generalisation, not as a production accuracy estimate.

### Other drawing metrics by set (+repair; model-only in brackets)

| Set | Envelope | Heat / surface treatment | Notes captured | Spurious tolerances |
|---|---|---|---|---|
| tuning | 100% (16/16) (100% (16/16)) | 100% (10/10) | 100% (16/16) | 0 (0) |
| holdout | 100% (10/10) (100% (10/10)) | 100% (6/6) | 100% (11/11) | 2 (2) |

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

    d39961621f696cdfc9eee25e6fa1b570  src/rfq_agent/llm/prompts/drawing_extract.v3.md
    118b03e91e0e139c16d759380171d51b  src/rfq_agent/llm/prompts/drawing_title_block.v2.md
    b9b18adef024d7f0ce12cbd505075f93  src/rfq_agent/llm/prompts/drawing_notes.v3.md
    4a3c082b12c7363a8d32d8bdbe4ee7b9  src/rfq_agent/llm/prompts/drawing_views.v4.md
    e029b19fcbba6885c0b89352ab09ff25  src/rfq_agent/llm/prompts/intake.v3.md
    7205c219475b9f06651b6bc7b635c2f0  src/rfq_agent/agents/drawing.py

## Tuning-set details (in-sample)

| Metric | Result | Target |
|---|---|---|
| Title-block field accuracy | 100% (50/50) | ≥ 90 % |
| Feature recall (type + nominal) | 96% (23/24) | ≥ 80 % |
| Feature precision | 96% (23/24) | — |
| Tolerance / fit accuracy | 90% (9/10) | — |
| Spurious tolerances on matched features | 0 | — |
| Heat / surface treatment | 100% (10/10) | — |
| Envelope (shape class + overall dims) | 100% (16/16) | — |
| Notes captured | 100% (16/16) | — |
| Parts-list rows fully correct (ASM-5100) | 100% (7/7) | — |
| Parts-list fields correct (ASM-5100) | 100% (35/35) | — |
| Email field accuracy | 100% (48/48) | — |
| Special requirements recall / precision | 100% (8/8) / 100% (8/8) | — |
| Extraction errors | 0 | 0 |

## Per drawing

| Set | Drawing | Title block | Recall model-only | Recall +repair | Precision +repair | Tolerance +repair | HT | ST | Envelope |
|---|---|---|---|---|---|---|---|---|---|
| tuning | SH-4711.pdf | 100% (10/10) | 43% (3/7) | 100% (7/7) | 100% (7/7) | 100% (4/4) | ok | ok | 100% (3/3) |
| tuning | FL-2208.pdf | 100% (10/10) | 71% (5/7) | 100% (7/7) | 100% (7/7) | 100% (2/2) | ok | ok | 100% (3/3) |
| tuning | BR-0930.pdf | 100% (10/10) | 100% (5/5) | 100% (5/5) | 100% (5/5) | 100% (1/1) | ok | ok | 100% (4/4) |
| tuning | ASM-5100.pdf | 100% (10/10) | n/a | n/a | n/a | n/a | ok | ok | 100% (3/3) |
| tuning | GR-3340.pdf | 100% (10/10) | 80% (4/5) | 80% (4/5) | 80% (4/5) | 67% (2/3) | ok | ok | 100% (3/3) |
| holdout | BS-60218.pdf | 100% (10/10) | 100% (7/7) | 100% (7/7) | 100% (7/7) | 100% (3/3) | ok | ok | 100% (3/3) |
| holdout | CP-7390.pdf | 100% (10/10) | 83% (5/6) | 83% (5/6) | 83% (5/6) | 67% (2/3) | ok | ok | 100% (4/4) |
| holdout | PS-2045.pdf | 100% (10/10) | 78% (7/9) | 78% (7/9) | 78% (7/9) | 100% (3/3) | ok | ok | 100% (3/3) |

**SH-4711.pdf** (tuning)

- model-only, fixed by repair: missed feature — outer_diameter 32.0 (Ø32 h7)
- model-only, fixed by repair: missed feature — outer_diameter 35.0 (Ø35 k6)
- model-only, fixed by repair: missed feature — outer_diameter 40.0 (Ø40)
- model-only, fixed by repair: missed feature — outer_diameter 35.0 (Ø35 k6)
- model-only, fixed by repair: extra feature — bore 32.0 (Ø32 h7)
- model-only, fixed by repair: extra feature — bore 35.0 (Ø35 k6)
- model-only, fixed by repair: extra feature — bore 35.0 (Ø35 k6)
- model-only, fixed by repair: tolerance — Ø32 h7: got feature not found
- model-only, fixed by repair: tolerance — Ø35 k6: got feature not found
- model-only, fixed by repair: tolerance — Ø35 k6: got feature not found

**FL-2208.pdf** (tuning)

- model-only, fixed by repair: missed feature — outer_diameter 120.0 (Ø120)
- model-only, fixed by repair: missed feature — chamfer 1.0 (1×45°)
- model-only, fixed by repair: extra feature — chamfer None (1x45°)

**GR-3340.pdf** (tuning)

- missed feature — keyway 8.0 (8 JS9)
- extra feature — bore 8.0 (8 J59)
- tolerance — 8 JS9: got feature not found

**CP-7390.pdf** (holdout)

- missed feature — bore 56.0 (Ø56 H8)
- extra feature — hole 56.0 (Ø56 H8)
- tolerance — Ø56 H8: got feature not found
- model-only, fixed by repair: missed feature — hole 20.0 (C'BORE Ø20 depth 13)
- model-only, fixed by repair: extra feature — bore 20.0 (C'BORE Ø20 depth 13)

**PS-2045.pdf** (holdout)

- missed feature — spline 25.0 (DIN 5480-W25×1.25×18×8f)
- missed feature — hole 3.15 (2× DIN 332-A3.15/6.7)
- extra feature — groove 3.15 (2x DIN 332-A3.15/6.7)
- extra feature — groove 1.25 (DIN 5480-W25x1.25x18x8f)

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

## Per email (tuning set)

| RFQ | Fields | Special requirements (recall) | Misses |
|---|---|---|---|
| RFQ-2026-0101 | 100% (12/12) | 100% (2/2) | — |
| RFQ-2026-0102 | 100% (12/12) | 100% (2/2) | — |
| RFQ-2026-0103 | 100% (12/12) | 100% (2/2) | — |
| RFQ-2026-0104 | 100% (12/12) | 100% (2/2) | — |

## Latency and tokens per call

Latency is wall time measured when the call was recorded (local Ollama on a 36 GB Apple-silicon laptop,
model already loaded); replay serves the same numbers from the cache.

| Task | Calls | Median latency s | Max latency s | Median input tok | Median output tok | Retries |
|---|---|---|---|---|---|---|
| intake | 4 | 10.1 | 10.5 | 1186 | 275 | 0 |
| drawing_title_block | 8 | 16.0 | 19.6 | 948 | 518 | 0 |
| drawing_notes | 8 | 6.2 | 18.7 | 1092 | 132 | 0 |
| drawing_views | 8 | 32.7 | 54.1 | 2611 | 1136 | 0 |

Per drawing (3 calls, sequential): median 53.5 s, max 80.9 s.

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
