REGION: title block (bottom-right corner of the sheet, ISO 7200 style).

Read every field of the title block: part number (drawing number), revision, title, material, general tolerance
(e.g. "ISO 2768-fH"), default surface roughness (e.g. "Ra 6.3" -> 6.3), scale, units (mm unless "inch" is printed),
drawn by, date.
- `material`: exactly as printed, including suffixes such as "+N". If the material field is empty, material = null
  and add the warning "Material field in title block is empty".
- For every field you fill, add an entry to `title_block.evidence` keyed by the field name with the verbatim printed text.
- Ignore any other parts of the sheet visible at the crop edges.
