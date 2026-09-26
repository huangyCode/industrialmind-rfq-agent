# Material Standards

Materials we stock or buy on short notice, their European designations and the rules for reading
material callouts on customer drawings. Prices and densities are kept in the materials master data.

## Material callout

A complete material callout on a drawing contains three elements:

1. Designation, by name or material number: `42CrMo4` or `1.7225`.
2. Standard: `EN 10083-3`.
3. Delivery or heat treatment condition: `+QT` (quenched and tempered), `+N` (normalised), `T6` (aluminium).

Example of a complete callout: `42CrMo4+QT EN 10083-3, 28-32 HRC`.

- An empty material field in the title block blocks the quote: material drives raw material cost,
  machinability (cycle time), heat treatment and plant selection. Ask the customer for designation and standard.
- A callout with designation but no standard is accepted when the designation is unambiguous
  (all materials in the table below); the standard is added to the quote assumptions.
- "Steel" or "aluminium" alone is not a material callout and is treated as missing.

## Stocked materials

| Code | Designation | Material no. | Standard | Group | Density (g/cm³) | Machinability index |
|---|---|---|---|---|---|---|
| C45 | C45 / C45E | 1.0503 | EN 10083-2 | steel | 7.85 | 1.00 |
| 42CrMo4 | 42CrMo4 | 1.7225 | EN 10083-3 | steel | 7.85 | 0.80 |
| 16MnCr5 | 16MnCr5 | 1.7131 | EN 10084 | case hardening steel | 7.85 | 0.85 |
| AW6082 | EN AW-6082 T6 (AlSi1MgMn) | 3.2315 | EN 573-3 / EN 755-2 | aluminium | 2.70 | 2.50 |
| GJS500 | EN-GJS-500-7 | 5.3200 | EN 1563 | cast iron | 7.10 | 1.10 |
| X5CrNi18-10 | X5CrNi18-10 | 1.4301 | EN 10088-3 | stainless | 7.90 | 0.50 |

The machinability index is relative to C45 (= 1.00); cycle times scale with 1 / index.

## Equivalents

Customers from other regions often use national designations. Accepted equivalents:

| Our code | EN / DIN | USA (AISI/SAE/ASTM) | Japan (JIS) | China (GB) | Old DIN |
|---|---|---|---|---|---|
| C45 | C45, 1.0503 | 1045 | S45C | 45 | CK45 (C45E) |
| 42CrMo4 | 42CrMo4, 1.7225 | 4140 | SCM440 | 42CrMo | – |
| 16MnCr5 | 16MnCr5, 1.7131 | 5115 | SCr415 (approx.) | 16MnCr5 | – |
| AW6082 | EN AW-6082, 3.2315 | 6082 (6351 similar) | – | 6082 | AlMgSi1 |
| GJS500 | EN-GJS-500-7, 5.3200 | ASTM A536 80-55-06 | FCD500 | QT500-7 | GGG-50 |
| X5CrNi18-10 | 1.4301 | 304 | SUS304 | 06Cr19Ni10 | V2A (trade name) |

- Equivalents are close but not identical in chemistry; for safety-relevant parts the customer must
  confirm the substitution in writing.
- A material that cannot be mapped to our master data is not a blocker: purchasing checks availability
  and price (typical answer within 2 working days), and the quote states the assumed equivalent.
- Materials we do not stock but can source: 11SMnPb30 (free-cutting), 34CrNiMo6, EN AW-7075, 1.4404.
  Minimum purchase quantity for non-stock bar is usually one bar length (3 – 6 m).

## Heat treatment

| Material | Treatment | Typical parameters | Result |
|---|---|---|---|
| C45 | Normalising (+N) | 840 – 880 °C, air cool | 170 – 210 HB |
| C45 | Induction hardening | surface, depth 1.5 – 3 mm | 52 – 58 HRC |
| 42CrMo4 | Quench and temper (+QT) | harden 820 – 860 °C oil, temper 540 – 680 °C | 28 – 32 HRC, Rm 900 – 1100 MPa |
| 16MnCr5 | Case hardening | carburise 880 – 980 °C, harden 810 – 840 °C, temper 150 – 200 °C | surface 58 – 62 HRC, CHD 0.6 – 1.0 mm |
| EN AW-6082 | T6 (as delivered) | solution treated and artificially aged | 95 – 100 HB, Rm ≥ 295 MPa |
| X5CrNi18-10 | Solution annealed (as delivered) | 1000 – 1100 °C, water | not hardenable |

- Quench and temper (service HT_QT) and case hardening (service HT_CASE) are outsourced to local
  heat treatment shops; add one week of lead time.
- Case hardening depth (CHD) is measured at 550 HV per ISO 18203; specify it together with surface hardness.
- 42CrMo4 can be bought pre-hardened (+QT, 28 – 32 HRC) as bar up to Ø100 mm, which removes the
  heat treatment step when no higher hardness is required.
- See dfm_guidelines.md, heat treatment sequence, for the order of operations.

## Surface treatment

| Treatment | Material | Layer | Notes |
|---|---|---|---|
| Anodizing (ANODIZE), type II | aluminium | 10 – 25 µm, colour clear or black | +0.01 – 0.02 mm per surface on dimensions |
| Hard anodizing | aluminium | 25 – 50 µm | on request, not a standard service |
| Zinc plating (ZINC) | steel | 8 – 12 µm, trivalent passivation | risk of hydrogen embrittlement above 39 HRC |
| Black oxide (BLACK) | steel | < 1 µm | cosmetic, low corrosion protection |

- Precision fits (IT7 and finer) must be masked or machined to size after coating.

## Material certificates

- Standard delivery includes a test report to EN 10204 2.2.
- An inspection certificate EN 10204 3.1 is available on request at €35 per batch and material heat.
