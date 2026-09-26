# Tolerance Guide

Reference for reading tolerances on customer drawings and for deciding which process is needed.
Values follow ISO 286-1 (limits and fits) and ISO 2768-1/-2 (general tolerances).

## IT grades

ISO 286-1 defines standard tolerance grades IT01 – IT18. The tolerance width depends on the
nominal size range. Standard tolerance values in µm:

| Nominal size (mm) | IT5 | IT6 | IT7 | IT8 | IT9 | IT10 | IT11 |
|---|---|---|---|---|---|---|---|
| > 3 – 6 | 5 | 8 | 12 | 18 | 30 | 48 | 75 |
| > 6 – 10 | 6 | 9 | 15 | 22 | 36 | 58 | 90 |
| > 10 – 18 | 8 | 11 | 18 | 27 | 43 | 70 | 110 |
| > 18 – 30 | 9 | 13 | 21 | 33 | 52 | 84 | 130 |
| > 30 – 50 | 11 | 16 | 25 | 39 | 62 | 100 | 160 |
| > 50 – 80 | 13 | 19 | 30 | 46 | 74 | 120 | 190 |
| > 80 – 120 | 15 | 22 | 35 | 54 | 87 | 140 | 220 |
| > 120 – 180 | 18 | 25 | 40 | 63 | 100 | 160 | 250 |
| > 180 – 250 | 20 | 29 | 46 | 72 | 115 | 185 | 290 |
| > 250 – 315 | 23 | 32 | 52 | 81 | 130 | 210 | 320 |

- The IT grade of a toleranced dimension is the smallest grade whose value is ≥ (upper − lower deviation).
- IT6 and finer on a diameter requires grinding; this is flagged so the customer can confirm the need.
  Bearing seats (k5, k6, m6) and precision pins (g6, h6) are typical legitimate uses.
- IT7 can be turned or reamed in stable production; IT8 – IT11 are normal turning and milling tolerances.

## Fits

A fit code combines a fundamental deviation letter with an IT grade: upper case for holes
(H7), lower case for shafts (k6). The digit is the IT grade.

| Fit | Type | Example (nominal) | Deviations (mm) |
|---|---|---|---|
| H7 | hole, basic | Ø80 H7 | +0.030 / 0 |
| H7 | hole, basic | Ø20 H7 | +0.021 / 0 |
| k6 | shaft, transition | Ø35 k6 | +0.018 / +0.002 |
| m6 | shaft, transition | Ø30 m6 | +0.021 / +0.008 |
| g6 | shaft, clearance | Ø25 g6 | −0.007 / −0.020 |
| h6 | shaft, basic | Ø40 h6 | 0 / −0.016 |
| h9 | shaft, basic | Ø40 h9 | 0 / −0.062 |
| P9 | keyway width | 10 P9 | −0.015 / −0.051 |

- Common pairings: H7/k6 for rolling bearing inner rings on rotating shafts, H7/g6 for sliding fits,
  H7/h6 for locating fits, H7/p6 for press fits.
- Bearing seats for deep groove ball bearings: shaft k6 (Ø18 – 100 mm), housing H7 or J7.

## General tolerances

ISO 2768 applies to all dimensions without an individual tolerance. The drawing must state the class
in or near the title block (e.g. "ISO 2768-mK"). Part 1 (first letter) covers linear and angular
dimensions; part 2 (second letter, H/K/L) covers geometric tolerances.

Linear dimensions, permissible deviations (mm):

| Nominal range (mm) | f (fine) | m (medium) | c (coarse) | v (very coarse) |
|---|---|---|---|---|
| 0.5 – 3 | ±0.05 | ±0.1 | ±0.2 | – |
| > 3 – 6 | ±0.05 | ±0.1 | ±0.3 | ±0.5 |
| > 6 – 30 | ±0.1 | ±0.2 | ±0.5 | ±1.0 |
| > 30 – 120 | ±0.15 | ±0.3 | ±0.8 | ±1.5 |
| > 120 – 400 | ±0.2 | ±0.5 | ±1.2 | ±2.5 |
| > 400 – 1000 | ±0.3 | ±0.8 | ±2.0 | ±4.0 |

Geometric tolerances ISO 2768-2, straightness and flatness (mm):

| Nominal length (mm) | H | K | L |
|---|---|---|---|
| ≤ 10 | 0.02 | 0.05 | 0.1 |
| > 10 – 30 | 0.05 | 0.1 | 0.2 |
| > 30 – 100 | 0.1 | 0.2 | 0.4 |
| > 100 – 300 | 0.2 | 0.4 | 0.8 |

Circular run-out: H 0.1 mm, K 0.2 mm, L 0.5 mm.

- If no general tolerance is stated, we quote on the basis of ISO 2768-m (medium, class K for geometry)
  and state this assumption in the quote. Dimensions without tolerance and without a general tolerance
  are otherwise undefined and must not be inspected against an arbitrary limit.

## Achievable accuracy per process

| Process | Economic IT grade | Best IT grade | Typical Ra (µm) |
|---|---|---|---|
| Sawing | IT14 – IT16 | IT12 | 12.5 – 25 |
| CNC turning | IT8 – IT10 | IT6 | 1.6 – 3.2 |
| CNC milling 3-axis | IT8 – IT10 | IT7 | 1.6 – 3.2 |
| CNC milling 5-axis | IT8 – IT9 | IT7 | 0.8 – 3.2 |
| Drilling | IT11 – IT13 | IT10 | 3.2 – 6.3 |
| Reaming | IT7 – IT8 | IT6 | 0.8 – 1.6 |
| Cylindrical grinding | IT6 – IT7 | IT5 | 0.4 – 0.8 |
| Surface grinding | IT6 – IT7 | IT5 | 0.4 – 0.8 |
| Honing / lapping | IT5 – IT6 | IT4 | 0.1 – 0.4 |
| Gear hobbing | quality 8 – 9 (ISO 1328) | quality 7 | 1.6 – 3.2 |

- Tolerances tighter than the economic grade of the planned process require an additional operation.
- On hardened surfaces (> 45 HRC) only grinding reaches IT6 – IT7.

## Inspection effort

- Up to two features at IT7 or finer: manual inspection with micrometer, bore gauge and plug gauges.
- Three or more features at IT7 or finer, or positional tolerances ≤ 0.05 mm: CMM inspection
  (routing rule R11), about 4 min per part plus 15 min programme setup.
- First article inspection (FAI) report on request adds one full CMM measurement per batch.
