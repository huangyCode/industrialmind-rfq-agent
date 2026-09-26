REGION: right column above the title block. It may contain a "Technical requirements / Notes" block, a gear data
table and, on assembly drawings, a parts list (item table). Any of them may be absent.

- `notes`: each numbered note / technical requirement as one string, verbatim (without the number).
- `heat_treatment`: the heat-treatment requirement if a note states one (e.g. "QT 22-26 HRC", "case hardened 55-60 HRC, CHD 0.5-0.8"); else null.
- `surface_treatment`: coating / finish if stated (e.g. "anodized clear", "zinc plated"); else null.
- `parts_list`: one entry per table row: item number, part number, description, quantity, material, standard (e.g. "DIN 6325", "ISO 7089").
  Copy each cell character by character exactly as printed — keep the original language and spelling, do not
  correct, translate or complete words. An empty cell is null. If there is no parts list, return an empty list.
- `gear_data`: if a gear data table is present (module m, number of teeth z, pitch diameter d, face width b,
  pressure angle, accuracy grade), copy its values; `evidence` = the module line as printed (e.g. "m = 3"). Else null.
- Ignore the title block and views if they are partly visible at the crop edges.
