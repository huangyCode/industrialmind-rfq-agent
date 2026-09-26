# DFM Guidelines for Machined Parts

Internal design-for-manufacturing (DFM) rules applied by the review step before a part is costed.
Each section states the limit, why it matters for cost or quality, and what to suggest to the customer.
Limits are for our standard equipment (see plant_capabilities.md); special processes may exceed them at extra cost.

## Thin walls

Walls below the minimum thickness deflect under cutting forces and clamping, chatter, and distort
when residual stress is released. Scrap rates rise sharply and cycle times double because of light cuts.

| Material group | Minimum wall (mm) | Preferred wall (mm) |
|---|---|---|
| Steel (C45, 42CrMo4, 16MnCr5) | 2.0 | ≥ 3.0 |
| Stainless steel (1.4301) | 2.0 | ≥ 3.0 |
| Aluminium (EN AW-6082) | 1.5 | ≥ 2.5 |
| Cast iron (EN-GJS-500-7) | 3.0 | ≥ 4.0 |

- Wall height to thickness ratio should stay below 15:1; above that, add ribs or reduce height.
- Thin walls next to deep pockets need finishing in several step-downs of max. 3 × wall thickness.
- Suggestion: increase the wall to the preferred value or confirm that the thin wall is functionally required.
  If it is required, quote an extra finishing pass and a flatness check on the CMM.

## Deep holes

The depth-to-diameter ratio (L/D) drives the drilling method:

| L/D | Method | Cost impact |
|---|---|---|
| ≤ 5 | Standard twist drill, one pass | none |
| 5 – 10 | Carbide drill with internal coolant, or peck drilling | +20 – 40 % drilling time |
| > 10 | Gun drilling or stepped drilling from both sides | +100 – 300 % drilling time, extra setup |
| > 30 | Gun drilling only, special machine | usually outsourced |

- Holes with L/D > 10 are flagged. Straightness of a gun-drilled hole is about 0.05 mm per 100 mm depth.
- Twist-drilled holes above L/D 10 tend to run off; positional tolerance better than ±0.2 mm cannot be held.
- Suggestion: reduce depth, increase the diameter, or allow a stepped hole (larger diameter where no function).
- Blind holes should end in a 118° drill point cone; a flat bottom needs an extra end-mill operation.

## Surface finish

Achievable roughness Ra (µm) per process under normal production conditions:

| Process | Typical Ra | Best Ra |
|---|---|---|
| Sawing | 12.5 – 25 | 6.3 |
| CNC turning, finishing pass | 1.6 – 3.2 | 0.8 |
| CNC milling, finishing pass | 1.6 – 3.2 | 0.8 |
| Drilling | 3.2 – 6.3 | 1.6 |
| Reaming | 0.8 – 1.6 | 0.4 |
| Cylindrical / surface grinding | 0.4 – 0.8 | 0.2 |
| Fine grinding, honing, lapping | 0.1 – 0.4 | 0.05 |

- Ra ≤ 0.8 µm requires grinding (routing rule R08). Ra ≤ 0.4 µm requires fine grinding, honing
  or lapping and is flagged: cost rises by a factor of 2 – 4 for that surface.
- Suggestion: specify Ra 0.8 µm on bearing seats and seal running surfaces unless the seal supplier
  requires finer; use Ra 3.2 µm as the default for non-functional surfaces.

## Heat treatment sequence

Heat treatment distorts parts (typically 0.02 – 0.10 mm on a shaft of 200 mm) and hardened surfaces
above 45 HRC cannot be turned economically. Required sequence when a part has both heat treatment and
precision tolerances (IT7 or finer, or Ra ≤ 0.8 µm):

1. Rough machining, leaving 0.2 – 0.3 mm grinding allowance on the diameter of precision features.
2. Heat treatment (quench and temper, or case hardening), outsourced, +1 week lead time.
3. Straightening if run-out exceeds 0.05 mm.
4. Grinding of the precision features to final size.

- Quench and temper to 28 – 32 HRC before machining (pre-hardened bar, 42CrMo4+QT) avoids step 2
  when the drawing does not require higher hardness; tolerances of IT7 can then be turned directly.
- Threads and keyways on case-hardened parts must be protected (copper plating or masking) or cut before hardening.

## Internal corners

Milled pockets always have an internal corner radius equal to at least the cutter radius.

- Minimum internal radius: 0.5 mm (Ø1 mm end mill, very slow). Preferred: ≥ 1/3 of pocket depth,
  and at least 3 mm for pockets deeper than 10 mm.
- Sharp internal corners (R0) require wire EDM or a separate slotting operation; they are flagged.
- A radius slightly larger than the standard cutter (e.g. R6.5 for a Ø12 mm cutter) gives smoother
  toolpaths and better surface finish than a radius equal to the cutter radius.
- Where a mating part has a sharp edge, suggest a relief (dog-bone) corner instead of a sharp one.

## Threads

- Tapped thread depth: 1.5 × D is sufficient for steel, 2 × D for aluminium and cast iron.
  Deeper threads add no strength and tapping deeper than 3 × D risks tap breakage; flagged.
- Blind tapped holes need a drill depth of at least thread depth + 3 thread pitches.
- Preferred sizes: M4, M5, M6, M8, M10, M12, M16 coarse pitch; fine pitch only when functionally required.
- Threads above M20 in steel are milled (thread milling) rather than tapped.
- Centre holes to DIN 332-2 form DS (e.g. DS M12) are drilled and tapped on the lathe.

## Keyways and splines

- Parallel keyways to DIN 6885-1: width tolerance P9 (fixed fit) or N9 (normal fit).
- Keyways are milled in a separate setup (routing rule R05); keyway end radius equals half the width.
- Splines and gear teeth require hobbing (HOB work center), which is not available in plant PL.

## General design advice

- Avoid undercuts that need special tools; use a standard relief groove to DIN 509 form E or F.
- Chamfer all external edges 0.5 × 45° unless otherwise specified; break internal edges 0.2 mm.
- Keep the number of setups low: features on one side of a prismatic part avoid 5-axis machining.
