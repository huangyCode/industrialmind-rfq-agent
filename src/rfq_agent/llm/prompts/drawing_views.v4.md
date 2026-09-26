REGION: drawing views (main view and section / side / detail views). Image 1 is the full sheet for orientation.
The following images are zoomed, non-overlapping tiles of the view area: read every dimension from the tiles.

`envelope`:
- `shape_class`: "rotational" (turned parts: shafts, flanges, bushes, gears, discs), "prismatic" (milled blocks,
  brackets, housings, plates) or "assembly" (several parts with item balloons / a parts list).
- rotational -> `max_diameter_mm` (largest outer Ø) and `length_mm` (overall length along the axis, or the
  thickness of a disc); prismatic -> `length_mm`, `width_mm`, `height_mm`. Null for any overall size not printed.

`features`: one entry per machined feature, ids F1, F2, ... in reading order (left to right, then section views).
First decide the part type, then classify each callout by what it describes:

Turned (rotational) parts:
- `outer_diameter`: EVERY Ø dimension of the outer contour — each shaft step, collar, flange rim, gear blank,
  INCLUDING the largest diameter (it is a turned surface and also goes to the envelope).
- `bore`: a Ø of an internal cylindrical surface made by turning / boring on the axis (centre bore of a flange,
  gear or bush, a spigot seat / recess).
- `hole`: drilled holes off the axis (bolt-circle holes, pin holes), through or blind.
- ISO fit letter case tells inside from outside: lower-case codes (f7, m6, js5, n6) are SHAFTS -> outer surface;
  upper-case codes (F8, G7, N9, D10) are HOLES / slots. A "Ø.. f7" is never a bore.
- `keyway`: a key slot, in a shaft OR in the hub of a bore (the small rectangular notch at the edge of a bore seen in
  the end view). Its width is printed with a slot tolerance class such as P9, N9 or JS9 (e.g. "6 N9" -> nominal_mm 6,
  fit "N9"). The dimension from the far side of the bore to the bottom of the hub keyway (a number slightly larger than
  the bore Ø) is the keyway depth, NOT a separate feature.

Milled (prismatic) parts:
- Every Ø callout on a block, bracket or plate is a `hole` (drilled or reamed), also when it carries a fit such as
  H7 or a depth. `bore` is only used for large cylindrical seats made by boring.
- `pocket`: milled recess. A callout "POCKET L×W depth D" gives two sizes: nominal_mm = the SMALLER one (width),
  length_mm = the LARGER one — e.g. "POCKET 90×35 depth 12" -> nominal_mm 35, length_mm 90.
  `slot`: same convention. `wall`: a thin wall with its own callout; nominal_mm = wall thickness.

All parts:
- `thread`: "M.." callouts. `thread_spec` e.g. "M16x1.5" as printed (just "M5" if no pitch is printed);
  nominal_mm = thread diameter; length_mm = thread depth.
- `chamfer`: "0.8x45°" -> nominal_mm 0.8; "2x 0.8x45°" -> nominal_mm 0.8, quantity 2.
- `groove`: undercut / recess groove.
- `face`: ONLY a length, thickness or step dimension that carries its own tolerance (e.g. "18 ±0.2" -> nominal_mm 18
  with upper 0.2, lower -0.2). A plain length without its own tolerance is not a feature.
- `gear_teeth`, `spline`: only when the teeth are dimensioned in the views; gear data tables are read elsewhere.

Values:
- `nominal_mm`: the number that follows Ø or M, or the width as described above — also when the callout starts with
  a count ("6x Ø11 THRU" -> nominal_mm 11, quantity 6; "3x M5 depth 10" -> nominal_mm 5, quantity 3, length_mm 10).
- `quantity`: the count prefix ("4x", "2x"); 1 otherwise. A dimension printed once with a count is ONE feature.
  Features dimensioned separately at different places are separate features, even when their size is the same.
- `length_mm`: section length, depth or keyway length only if dimensioned ("depth 18" -> 18; THRU -> part thickness if printed).
- `tolerance`: only when the dimension itself carries a fit code or deviations; null for plain dimensions.
  Tolerance classes can have two letters (JS7, js5, ZC): copy all letters.
- `ra_um`: only if a roughness symbol points at that feature ("Ra 0.4" -> 0.4). A roughness symbol is never a feature itself.
- `evidence.text`: the verbatim callout, e.g. "Ø22 g6", "6x Ø11 THRU", "M4 depth 8"; `evidence.location` e.g. "side view", "section C-C".

Do NOT list as features: overall lengths / widths / heights (they go to the envelope), plain position / distance
dimensions without their own tolerance, section-line letters, roughness symbols, item balloons, or the same callout
seen twice.
ASSEMBLY drawings: return an EMPTY features list — machining features belong to the component drawings; fill only
the envelope from the overall dimensions.
