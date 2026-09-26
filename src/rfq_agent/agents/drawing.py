"""Drawing Agent: render the PDF, crop fixed layout regions, extract each region with the VLM, merge, post-process.

Region crops follow the shared sheet layout (A3 landscape 420×297 mm): title block bottom-right
(x 235–410, y 10–66 mm from the bottom), parts list / notes stacked above it in the same right column,
views in the left area (x 20–230 mm). Fractions below add a few mm of margin so small layout drift is tolerated.
Three model calls per drawing: title block, right column (notes, gear data, parts list), views.

Local VLMs downsample every image to a fixed visual-token budget (gemma4 via Ollama: ~280 tokens per image,
whatever the pixel size), so small dimension text in one big view crop becomes unreadable. The views call
therefore gets the full sheet for orientation plus up to four content tiles: the view area is trimmed to its
drawn content and split only along empty bands, so no view, leader or callout is cut in half.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pymupdf
from pydantic import BaseModel, Field

from rfq_agent.agents.postprocess import postprocess
from rfq_agent.config import RENDER_DIR
from rfq_agent.data.repositories import MaterialRepo
from rfq_agent.llm.client import ExtractionError, LLMClient, load_prompt
from rfq_agent.models import (
    DrawingSpec,
    Envelope,
    Evidence,
    Feature,
    FeatureType,
    PartsListItem,
    ShapeClass,
    TitleBlock,
)

# per-region prompt versions; "base" is the shared drawing_extract prompt prepended to every region prompt
PROMPT_VERSIONS = {"base": "v3", "title_block": "v2", "notes": "v3", "views": "v4"}
PROMPT_VERSION = PROMPT_VERSIONS["views"]  # headline version, kept for callers that log one value
SHEET_W_MM, SHEET_H_MM = 420.0, 297.0

# (x0, y0, x1, y1) in mm, y measured from the TOP of the sheet (image coordinates)
REGIONS_MM: dict[str, tuple[float, float, float, float]] = {
    "title_block": (228, 297 - 72, 416, 294),
    "right_column": (228, 12, 409, 297 - 64),  # notes + parts list, inside the sheet frame
    "views": (12, 3, 238, 294),
}
# inside the sheet frame (frame at x 20 / y 10 mm; centring ticks reach 25 mm into the sheet on the left)
VIEWS_INNER_MM = (26, 12, 234, 285)
OVERVIEW_MAX_PX = 1600
MAX_TILES = 4
TILE_SPLIT_MM = 110  # split a tile whose longer side exceeds this
MIN_GAP_MM = 3  # an empty band at least this wide separates two views
MARGIN_MM = 4


class TitleBlockExtract(BaseModel):
    title_block: TitleBlock
    extraction_warnings: list[str] = Field(default_factory=list)


class GearData(BaseModel):
    module: float | None = Field(None, description="Module m in mm")
    teeth: int | None = Field(None, description="Number of teeth z")
    pitch_diameter_mm: float | None = Field(None, description="Pitch diameter d")
    face_width_mm: float | None = Field(None, description="Face width b")
    pressure_angle_deg: float | None = None
    accuracy: str | None = Field(None, description="e.g. 'ISO 1328-1 grade 7'")
    evidence: str | None = Field(None, description="Module line as printed, e.g. 'm = 3'")


class NotesExtract(BaseModel):
    notes: list[str] = Field(default_factory=list)
    heat_treatment: str | None = Field(None, description="e.g. 'QT 28-32 HRC', 'case hardened 58-62 HRC'")
    surface_treatment: str | None = Field(None, description="e.g. 'anodized black', 'zinc plated'")
    parts_list: list[PartsListItem] = Field(default_factory=list)
    gear_data: GearData | None = None
    extraction_warnings: list[str] = Field(default_factory=list)


class ViewsExtract(BaseModel):
    envelope: Envelope
    features: list[Feature] = Field(default_factory=list)
    extraction_warnings: list[str] = Field(default_factory=list)


def render_pdf(pdf: Path, dpi: int = 200, out_dir: Path = RENDER_DIR) -> list[Path]:
    """Render every page to PNG; returns the page image paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            p = out_dir / f"{Path(pdf).stem}_p{i}.png"
            page.get_pixmap(dpi=dpi).save(p)
            paths.append(p)
    return paths


def _crop(
    page_png: Path, frac: tuple[float, float, float, float], out: Path, max_px: int | None = None
) -> Path:
    with pymupdf.open(page_png) as img:
        page = img[0]
        full = pymupdf.Pixmap(page_png)
        scale = full.width / page.rect.width  # render at the PNG's native pixel density
        r = page.rect
        clip = pymupdf.Rect(
            r.x0 + frac[0] * r.width,
            r.y0 + frac[1] * r.height,
            r.x0 + frac[2] * r.width,
            r.y0 + frac[3] * r.height,
        )
        if max_px:
            scale = min(scale, max_px / max(clip.width, clip.height))
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip).save(out)
    return out


def _ink(page_png: Path) -> np.ndarray:
    """Boolean ink mask of the page (True = dark pixel)."""
    pix = pymupdf.Pixmap(page_png)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return a[..., :3].min(axis=2) < 160


def _bbox(mask: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = box
    sub = mask[y0:y1, x0:x1]
    rows, cols = np.flatnonzero(sub.any(axis=1)), np.flatnonzero(sub.any(axis=0))
    if not len(rows):
        return None
    return x0 + cols[0], y0 + rows[0], x0 + cols[-1] + 1, y0 + rows[-1] + 1


def _split(mask: np.ndarray, box: tuple[int, int, int, int], px_mm: float) -> list[tuple[int, int, int, int]]:
    """Split a content box in two along the empty band (rows or columns) closest to its middle."""
    x0, y0, x1, y1 = box
    axis = 1 if (y1 - y0) >= (x1 - x0) else 0  # split the longer side
    filled = mask[y0:y1, x0:x1].any(axis=axis)
    n, mid, best = len(filled), len(filled) / 2, None
    i = 0
    while i < n:
        if filled[i]:
            i += 1
            continue
        j = i
        while j < n and not filled[j]:
            j += 1
        if j - i >= MIN_GAP_MM * px_mm and 0.2 * n < (i + j) / 2 < 0.8 * n:
            c = (i + j) // 2
            if best is None or abs(c - mid) < abs(best - mid):
                best = c
        i = j
    if best is None:
        return [box]
    parts = (
        [(x0, y0, x1, y0 + best), (x0, y0 + best, x1, y1)]
        if axis == 1
        else [
            (x0, y0, x0 + best, y1),
            (x0 + best, y0, x1, y1),
        ]
    )
    return [b for p in parts if (b := _bbox(mask, p))]


def view_tiles(page_png: Path) -> list[tuple[int, int, int, int]]:
    """Pixel boxes of the drawn content in the view area, split along empty bands into at most MAX_TILES."""
    mask = _ink(page_png)
    h, w = mask.shape
    px_mm = w / SHEET_W_MM
    x0, y0, x1, y1 = (int(v * px_mm) for v in VIEWS_INNER_MM)
    root = _bbox(mask, (x0, y0, x1, min(y1, h)))
    if root is None:
        return []
    tiles = [root]
    while len(tiles) < MAX_TILES:
        big = max(tiles, key=lambda b: max(b[2] - b[0], b[3] - b[1]))
        if max(big[2] - big[0], big[3] - big[1]) < TILE_SPLIT_MM * px_mm:
            break
        parts = _split(mask, big, px_mm)
        if len(parts) < 2:
            break
        tiles.remove(big)
        tiles += parts
    tiles.sort(
        key=lambda b: (round(b[1] / (40 * px_mm)), b[0])
    )  # reading order: rows of views, then left to right
    m = int(MARGIN_MM * px_mm)
    return [(max(a - m, 0), max(b - m, 0), min(c + m, w), min(d + m, h)) for a, b, c, d in tiles]


def _crop_px(page_png: Path, box: tuple[int, int, int, int], out: Path) -> Path:
    pix = pymupdf.Pixmap(page_png)
    x0, y0, x1, y1 = box
    sub = pymupdf.Pixmap(pix, pix.width, pix.height, pymupdf.IRect(x0, y0, x1, y1))
    sub.save(out)
    return out


def crop_regions(page_png: Path, client: LLMClient | None = None) -> dict[str, Path]:
    """Crops: title_block, right_column (trimmed to content), views (fixed area), view_1..view_n content tiles,
    and a downscaled full-sheet overview.

    `client` is accepted for the contract (a variant could ask the model for view boxes); tiles are found from
    the drawn content instead, which is deterministic and costs no model call.
    """
    page_png = Path(page_png)
    out: dict[str, Path] = {}
    for name, (x0, y0, x1, y1) in REGIONS_MM.items():
        frac = (x0 / SHEET_W_MM, y0 / SHEET_H_MM, x1 / SHEET_W_MM, y1 / SHEET_H_MM)
        out[name] = _crop(page_png, frac, page_png.with_name(f"{page_png.stem}_{name}.png"))
    # the right column is mostly empty paper above the notes; trim it so the text gets more of the image budget
    rc = out["right_column"]
    if (b := _bbox(_ink(rc), (0, 0, *pymupdf.Pixmap(rc).irect[2:]))) is not None:
        px_mm = pymupdf.Pixmap(page_png).width / SHEET_W_MM
        m = int(MARGIN_MM * px_mm)
        pw, ph = pymupdf.Pixmap(rc).width, pymupdf.Pixmap(rc).height
        _crop_px(rc, (max(b[0] - m, 0), max(b[1] - m, 0), min(b[2] + m, pw), min(b[3] + m, ph)), rc)
    for i, box in enumerate(view_tiles(page_png), start=1):
        out[f"view_{i}"] = _crop_px(page_png, box, page_png.with_name(f"{page_png.stem}_view_{i}.png"))
    out["overview"] = _crop(
        page_png, (0, 0, 1, 1), page_png.with_name(f"{page_png.stem}_overview.png"), max_px=OVERVIEW_MAX_PX
    )
    return out


def _system(region: str) -> str:
    return (
        load_prompt("drawing_extract", PROMPT_VERSIONS["base"])
        + "\n\n"
        + load_prompt(f"drawing_{region}", PROMPT_VERSIONS[region])
    )


def _version(region: str) -> str:
    return f"{PROMPT_VERSIONS[region]}+base.{PROMPT_VERSIONS['base']}"


_NUM = r"(\d+(?:[.,]\d+)?)"
_COUNT_RE = re.compile(r"^\s*(\d+)\s*[x×]\s+")
_SIZE_RE = re.compile(rf"(?:[Ø⌀]|\bM)\s*{_NUM}")
_CHAMFER_RE = re.compile(rf"{_NUM}\s*[x×]\s*45")


def _num(s: str) -> float:
    return float(s.replace(",", "."))


_PAIR_RE = re.compile(rf"{_NUM}\s*[x×]\s*{_NUM}")
_INTERNAL = (FeatureType.BORE, FeatureType.HOLE)
_KEYWAY_FIT_RE = re.compile(r"(?i)(?:N|P|JS|D)(?:9|10)")


def _fit_case(fit: str | None) -> str | None:
    """'shaft' for lower-case ISO 286 classes (h7, js6), 'hole' for upper-case (H7, P9), None if mixed/absent."""
    letters = re.sub(r"[^A-Za-z]", "", fit or "")
    if not letters:
        return None
    if letters.islower():
        return "shaft"
    return "hole" if letters.isupper() else None


def _classify(f: Feature, shape: ShapeClass | None, text: str) -> Feature:
    """Enforce drawing conventions a small VLM often ignores when choosing the feature type."""
    fit = f.tolerance.fit if f.tolerance else None
    has_dia = bool(re.search(r"[Ø⌀]", text))
    # a width with a key-slot tolerance class (N9, P9, JS9, D10) and no Ø sign is a keyway (shaft or hub)
    if f.type in _INTERNAL and fit and _KEYWAY_FIT_RE.fullmatch(fit.strip()) and text and not has_dia:
        f.type = FeatureType.KEYWAY
    if f.type == FeatureType.KEYWAY and fit:
        f.tolerance.fit = fit.upper()  # keyway widths are toleranced as the "hole" (P9, N9, JS9)
    # ISO 286: lower-case class = shaft (outer surface), upper-case = hole
    case = _fit_case(f.tolerance.fit if f.tolerance else None)
    if f.type in _INTERNAL and case == "shaft" and shape == ShapeClass.ROTATIONAL:
        f.type = FeatureType.OUTER_DIAMETER
    elif f.type == FeatureType.OUTER_DIAMETER and case == "hole":
        f.type = FeatureType.BORE
    # on milled parts every Ø callout is a drilled / reamed hole
    if f.type == FeatureType.BORE and shape == ShapeClass.PRISMATIC:
        f.type = FeatureType.HOLE
    # "POCKET L×W": nominal = width (smaller), length = larger
    if f.type in (FeatureType.POCKET, FeatureType.SLOT) and (m := _PAIR_RE.search(text)):
        a, b = sorted((_num(m.group(1)), _num(m.group(2))))
        if f.nominal_mm is None or abs(f.nominal_mm - b) < 1e-6:
            f.nominal_mm, f.length_mm = a, b
    return f


def repair_features(
    features: list[Feature], envelope: Envelope | None = None
) -> tuple[list[Feature], list[str]]:
    """Deterministic clean-up of the model's feature list; returns (features, warnings).

    Fills values the model left empty but printed in its own evidence (count prefix, Ø / M size, chamfer),
    enforces type conventions (fit letter case, keyway vs bore, holes on prismatic parts, pocket width/length),
    drops `face` entries without their own tolerance (the prompt's definition of a face feature) and adds the
    largest turned Ø of a rotational part when the model put it only into the envelope.
    """
    shape = envelope.shape_class if envelope else None
    out: list[Feature] = []
    warnings: list[str] = []
    for f in features:
        f = f.model_copy(deep=True)
        text = f.evidence.text if f.evidence else ""
        if f.quantity == 1 and (m := _COUNT_RE.match(text)):
            f.quantity = int(m.group(1))
        if f.nominal_mm is None:
            if f.type == FeatureType.CHAMFER and (m := _CHAMFER_RE.search(text)):
                f.nominal_mm = _num(m.group(1))
            elif m := _SIZE_RE.search(text):
                f.nominal_mm = _num(m.group(1))
        if (
            f.type == FeatureType.THREAD
            and not f.thread_spec
            and (m := re.search(r"\bM\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?", text))
        ):
            f.thread_spec = re.sub(r"\s+", "", m.group(0)).replace("×", "x")
        t = f.tolerance
        if t is not None and t.fit and t.upper == 0 and t.lower == 0:
            t.upper = t.lower = None  # a fit code, not a zero band
        if t is not None and not t.fit and t.upper is None and t.lower is None:
            f.tolerance = None
        f = _classify(f, shape, text)
        if f.type == FeatureType.FACE and f.tolerance is None and f.ra_um is None:
            warnings.append(f"Dropped untoleranced face dimension '{text or f.nominal_mm}' (not a feature).")
            continue
        out.append(f)
    d = envelope.max_diameter_mm if envelope else None
    if (
        shape == ShapeClass.ROTATIONAL
        and d
        and not any(
            f.type == FeatureType.OUTER_DIAMETER and f.nominal_mm and abs(f.nominal_mm - d) <= 0.5
            for f in out
        )
    ):
        out.insert(
            0,
            Feature(
                id="F0",
                type=FeatureType.OUTER_DIAMETER,
                description=f"Largest outer diameter Ø{d:g} (from the envelope)",
                nominal_mm=d,
                length_mm=None,
                evidence=Evidence(text=f"Ø{d:g}", location="envelope (overall diameter)"),
            ),
        )
        warnings.append(f"Largest outer diameter Ø{d:g} added from the envelope.")
    for i, f in enumerate(out, start=1):
        f.id = f"F{i}"
    return out, warnings


_SCALE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)")


def check_envelope(env: Envelope, scale: str | None) -> tuple[Envelope, list[str]]:
    """Null overall sizes that cannot fit on the sheet at the printed scale (typically a misread extra digit)."""
    m = _SCALE_RE.search(scale or "")
    factor = _num(m.group(2)) / _num(m.group(1)) if m and _num(m.group(1)) > 0 else 1.0
    limit = SHEET_W_MM * factor
    env = env.model_copy()
    warnings = []
    for f in ("max_diameter_mm", "length_mm", "width_mm", "height_mm"):
        v = getattr(env, f)
        if v is not None and v > limit:
            setattr(env, f, None)
            warnings.append(
                f"Envelope {f} = {v:g} mm cannot fit on the A3 sheet at scale {scale or '1:1'}; "
                "treated as a misread and left empty — check the overall dimension."
            )
    return env, warnings


def gear_feature(g: GearData, next_id: int) -> Feature | None:
    if not (g.module and g.teeth):
        return None
    return Feature(
        id=f"F{next_id}",
        type=FeatureType.GEAR_TEETH,
        description=f"Gear teeth m={g.module:g}, z={g.teeth}" + (f", {g.accuracy}" if g.accuracy else ""),
        nominal_mm=g.pitch_diameter_mm or round(g.module * g.teeth, 3),
        gear_module=g.module,
        gear_teeth=g.teeth,
        face_width_mm=g.face_width_mm,
        evidence=Evidence(text=g.evidence or f"m = {g.module:g}", location="gear data table"),
    )


def extract_drawing(
    pdf: Path, client: LLMClient, materials: MaterialRepo, rfq_id: str | None = None
) -> tuple[DrawingSpec, list[Path]]:
    """Extract one drawing into a post-processed DrawingSpec. Returns (spec, page render paths)."""
    pdf = Path(pdf)
    pages = render_pdf(pdf)
    crops = crop_regions(pages[0], client)
    warnings: list[str] = []
    if len(pages) > 1:
        warnings.append(f"Drawing has {len(pages)} sheets; only sheet 1 was extracted.")

    def call(region: str, images: list[Path], schema, hint: str):
        return client.structured(
            task=f"drawing_{region}",
            prompt_version=_version(region),
            system=_system(region),
            user_text=f"Drawing file: {pdf.name}\n{hint}",
            images=images,
            schema=schema,
            rfq_id=rfq_id,
        )

    try:
        tb = call("title_block", [crops["title_block"]], TitleBlockExtract, "Image: title block crop.")
    except ExtractionError as e:
        tb = TitleBlockExtract(
            title_block=TitleBlock(), extraction_warnings=[f"Title block not extracted: {e}"]
        )
    try:
        notes = call(
            "notes", [crops["right_column"]], NotesExtract, "Image: right column above the title block."
        )
    except ExtractionError as e:
        notes = NotesExtract(extraction_warnings=[f"Notes / parts list not extracted: {e}"])
    # views are essential (envelope + features): let a failure propagate
    tiles = [crops[k] for k in sorted(crops) if k.startswith("view_")] or [crops["views"]]
    views = call(
        "views",
        [crops["overview"], *tiles],
        ViewsExtract,
        f"Image 1: full sheet (overview). Images 2-{len(tiles) + 1}: {len(tiles)} zoomed tile(s) of the view area.",
    )
    features, repair_warnings = repair_features(views.features, views.envelope)
    warnings += repair_warnings
    if views.envelope.shape_class == ShapeClass.ASSEMBLY or notes.parts_list:
        if features:
            warnings.append(
                f"Assembly drawing: {len(features)} feature(s) read from the views were dropped; "
                "machining features come from the component drawings."
            )
        features = []
    elif notes.gear_data and not any(f.type == FeatureType.GEAR_TEETH for f in features):
        if gf := gear_feature(notes.gear_data, len(features) + 1):
            features.append(gf)

    envelope, env_warnings = check_envelope(views.envelope, tb.title_block.scale)
    warnings += env_warnings
    spec = DrawingSpec(
        drawing_file=pdf.name,
        title_block=tb.title_block,
        envelope=envelope,
        features=features,
        heat_treatment=notes.heat_treatment,
        surface_treatment=notes.surface_treatment,
        notes=notes.notes,
        parts_list=notes.parts_list,
        extraction_warnings=warnings
        + tb.extraction_warnings
        + notes.extraction_warnings
        + views.extraction_warnings,
    )
    return postprocess(spec, materials), pages
