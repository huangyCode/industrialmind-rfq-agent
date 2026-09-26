"""Generate the synthetic sample RFQs: A3 drawings (PDF + PNG preview), emails and gold JSON.

Each part is defined once (title block, features, notes, parts list, geometry parameters). The same
definition produces the drawing and the gold ``DrawingSpec``: every feature's evidence text is the exact
string printed on the sheet, so the gold data cannot drift from the drawing.

Sheet layout (all parts): A3 landscape 420x297 mm, border 10 mm (20 mm filing margin on the left).
Right column x in [235, 410]: title block at the bottom (y in [10, 66]), parts list directly above it
(assemblies), technical requirements above that. Views live in the left area x in [20, 230].

Tuning set: the four RFQ samples + GR-3340 (data/samples/RFQ-*, eval_only). Hold-out set: BS-60218, CP-7390,
PS-2045 (data/samples/holdout), added after the prompts / repair rules were frozen, drawn in the "alt" layout
variant (other title-block field order and labels, smaller dimension text) inside the same sheet zones.

Usage: ``uv run python scripts/gen_drawings.py``
"""

from __future__ import annotations

import json
import math
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pymupdf  # noqa: E402
from matplotlib.patches import Arc, Circle, Polygon, Rectangle  # noqa: E402

from rfq_agent.config import SAMPLES_DIR  # noqa: E402
from rfq_agent.models import (  # noqa: E402
    DrawingSpec,
    Envelope,
    Evidence,
    Feature,
    FeatureType,
    PartsListItem,
    RFQItem,
    RFQRequest,
    ShapeClass,
    TitleBlock,
    Tolerance,
)

# ---------------------------------------------------------------- sheet constants (mm / pt)
SHEET_W, SHEET_H = 420.0, 297.0
BORDER = (20.0, 10.0, 410.0, 287.0)  # x0, y0, x1, y1
RIGHT_X0, RIGHT_X1 = 235.0, 410.0
TB_Y0, TB_Y1 = 10.0, 66.0
LEFT_X0, LEFT_X1 = 20.0, 230.0
BLOCK_GAP = 5.0

DIM_PT = 11.0
NOTE_PT = 10.5
ALT_DIM_PT = 10.0  # hold-out layout variant: dimension text one step smaller
ALT_NOTE_PT = 10.0
TB_LABEL_PT = 10.0
TB_VALUE_PT = 13.0
LW_THICK = 1.8
LW_THIN = 0.8
INK = "black"
PREVIEW_DPI = 150

plt.rcParams.update(
    {
        "pdf.fonttype": 42,  # embed TrueType so the text layer is extractable
        "font.family": "DejaVu Sans",
        "hatch.linewidth": 0.6,
        "hatch.color": INK,
    }
)

OUR_SALES = "Vertrieb PrecisionMotion <sales@precisionmotion.example>"


# ---------------------------------------------------------------- drawing primitives
class Sheet:
    """An A3 sheet whose axes use millimetre coordinates (1 data unit = 1 mm on paper)."""

    def __init__(self, dim_pt: float = DIM_PT) -> None:
        self.dim_pt = dim_pt  # dimension / callout text size
        self.fig = plt.figure(figsize=(SHEET_W / 25.4, SHEET_H / 25.4))
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.set_xlim(0, SHEET_W)
        self.ax.set_ylim(0, SHEET_H)
        self.ax.set_aspect("equal")
        self.ax.axis("off")

    # -- lines
    def line(self, pts, lw: float = LW_THICK, ls: str = "-", z: float = 3) -> None:
        xs, ys = zip(*pts, strict=True)
        self.ax.plot(xs, ys, color=INK, lw=lw, ls=ls, solid_capstyle="butt", zorder=z)

    def thin(self, pts, z: float = 3) -> None:
        self.line(pts, lw=LW_THIN, z=z)

    def hidden(self, pts) -> None:
        self.ax.plot(*zip(*pts, strict=True), color=INK, lw=1.0, ls=(0, (4, 2)), zorder=3)

    def center(self, pts) -> None:
        self.ax.plot(*zip(*pts, strict=True), color=INK, lw=0.6, ls=(0, (12, 3, 2, 3)), zorder=2)

    def rect(self, x0, y0, x1, y1, lw: float = LW_THICK, fill: str | None = None, z: float = 3) -> None:
        self.ax.add_patch(
            Rectangle(
                (x0, y0),
                x1 - x0,
                y1 - y0,
                fill=fill is not None,
                facecolor=fill or "none",
                edgecolor=INK,
                lw=lw,
                zorder=z,
            )
        )

    def circle(self, c, r, lw: float = LW_THICK, ls: str = "-", fill: str | None = None, z: float = 3):
        self.ax.add_patch(
            Circle(
                c,
                r,
                fill=fill is not None,
                facecolor=fill or "none",
                edgecolor=INK,
                lw=lw,
                ls=ls,
                zorder=z,
            )
        )

    def center_circle(self, c, r) -> None:
        self.ax.add_patch(Circle(c, r, fill=False, edgecolor=INK, lw=0.6, ls=(0, (12, 3, 2, 3)), zorder=2))

    def cross(self, c, half: float) -> None:
        x, y = c
        self.center([(x - half, y), (x + half, y)])
        self.center([(x, y - half), (x, y + half)])

    def hatch(self, pts, pattern: str = "///", z: float = 2) -> None:
        self.ax.add_patch(
            Polygon(pts, closed=True, fill=False, hatch=pattern, edgecolor=INK, lw=LW_THICK, zorder=z)
        )

    def thread_end(self, c, r_major) -> None:
        """Tapped hole seen end-on: thick minor circle + thin 3/4 major arc."""
        self.circle(c, r_major * 0.83)
        self.ax.add_patch(Arc(c, 2 * r_major, 2 * r_major, theta1=0, theta2=270, lw=LW_THIN, zorder=3))

    # -- text
    def text(self, x, y, s, size: float | None = None, ha="center", va="center", weight="normal", **kw):
        return self.ax.text(
            x,
            y,
            s,
            fontsize=self.dim_pt if size is None else size,
            ha=ha,
            va=va,
            weight=weight,
            color=INK,
            zorder=6,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.6},
            **kw,
        )

    def arrow_head(self, tip, toward, length: float = 3.2, width: float = 1.1) -> None:
        """Filled arrowhead with its point at ``tip``, pointing away from ``toward``."""
        tx, ty = tip
        dx, dy = tip[0] - toward[0], tip[1] - toward[1]
        n = math.hypot(dx, dy) or 1.0
        ux, uy = dx / n, dy / n
        bx, by = tx - ux * length, ty - uy * length
        px, py = -uy * width, ux * width
        self.ax.add_patch(
            Polygon([(tx, ty), (bx + px, by + py), (bx - px, by - py)], closed=True, color=INK, zorder=5)
        )

    # -- dimensions
    def hdim(self, x1, x2, y, text, ext_from: tuple[float, float] | None = None, tx=None) -> None:
        """Horizontal dimension at height y; extension lines from the given feature heights."""
        if ext_from:
            for x, yf in ((x1, ext_from[0]), (x2, ext_from[1])):
                self.thin([(x, yf + (1.0 if y > yf else -1.0)), (x, y + (2.0 if y > yf else -2.0))])
        self.thin([(x1, y), (x2, y)])
        if abs(x2 - x1) >= 9:
            self.arrow_head((x1, y), (x2, y))
            self.arrow_head((x2, y), (x1, y))
        else:  # small span: arrows outside
            self.thin([(x1 - 6, y), (x1, y)])
            self.thin([(x2, y), (x2 + 6, y)])
            self.arrow_head((x1, y), (x1 - 5, y))
            self.arrow_head((x2, y), (x2 + 5, y))
        self.text((x1 + x2) / 2 if tx is None else tx, y + 0.8, text, va="bottom")

    def vdim(self, x, y1, y2, text, ext_from: tuple[float, float] | None = None, side="right", ty=None):
        """Vertical dimension at x; text horizontal (unidirectional) beside the dimension line."""
        if ext_from:
            for y, xf in ((y1, ext_from[0]), (y2, ext_from[1])):
                sgn = 1.0 if x > xf else -1.0
                self.thin([(xf + sgn * 1.0, y), (x + sgn * 2.0, y)])
        self.thin([(x, y1), (x, y2)])
        self.arrow_head((x, y1), (x, y2))
        self.arrow_head((x, y2), (x, y1))
        ty = (y1 + y2) / 2 if ty is None else ty
        if side == "right":
            self.text(x + 1.5, ty, text, ha="left")
        else:
            self.text(x - 1.5, ty, text, ha="right")

    def diam_up(self, x, y_lo, y_hi, y_text, text) -> None:
        """Diameter across a shaft section, dimension line carried up to the text above the part."""
        self.thin([(x, y_lo), (x, y_text)])
        self.arrow_head((x, y_lo), (x, y_hi))
        self.arrow_head((x, y_hi), (x, y_lo))
        self.text(x, y_text + 0.3, text, va="bottom")

    def leader(self, target, knee, text_lines: list[str], side="right", dot=False) -> None:
        """Leader with arrowhead (or dot) at target, horizontal shoulder and text above the shoulder."""
        width = max(len(t) for t in text_lines) * 2.5 + 3
        end = (knee[0] + width, knee[1]) if side == "right" else (knee[0] - width, knee[1])
        self.thin([target, knee, end])
        if dot:
            self.circle(target, 0.8, lw=0.5, fill=INK, z=5)
        else:
            self.arrow_head(target, knee)
        tx = knee[0] + 1 if side == "right" else end[0] + 1
        for i, t in enumerate(reversed(text_lines)):  # first line on top
            self.text(tx, knee[1] + 0.8 + i * 5.4, t, ha="left", va="bottom")

    def roughness(self, target, knee, text: str, side="right") -> None:
        """Leader to a surface with an ISO 1302-style roughness symbol sitting on the shoulder."""
        length = 24.0
        end = (knee[0] + length, knee[1]) if side == "right" else (knee[0] - length, knee[1])
        self.thin([target, knee, end])
        self.arrow_head(target, knee)
        sx = min(knee[0], end[0]) + 3.0
        sy = knee[1]
        self.line([(sx - 2.2, sy + 3.2), (sx, sy), (sx + 4.4, sy + 7.5)], lw=1.0)
        self.text(sx + 4.2, sy + 0.6, text, ha="left", va="bottom")

    def cut_marker(self, x, y, letter: str, direction: tuple[float, float]) -> None:
        """Cutting-plane end mark: short thick stroke, viewing arrow and letter."""
        self.line([(x, y - 3), (x, y + 3)], lw=2.4)
        ax_, ay_ = x + direction[0] * 7, y + direction[1] * 7
        self.thin([(x, y), (ax_, ay_)])
        self.arrow_head((ax_, ay_), (x, y))
        self.text(ax_ + direction[0] * 3.5, ay_ + 3.5, letter, size=12, weight="bold")

    def view_label(self, x, y, text) -> None:
        self.text(x, y, text, size=12, weight="bold")

    def balloon(self, target, c, n: int) -> None:
        dx, dy = target[0] - c[0], target[1] - c[1]
        d = math.hypot(dx, dy)
        start = (c[0] + dx / d * 4.5, c[1] + dy / d * 4.5)
        self.thin([start, target])
        self.circle(target, 0.8, lw=0.5, fill=INK, z=5)
        self.circle(c, 4.5, lw=LW_THIN, fill="white", z=5)
        self.ax.text(c[0], c[1], str(n), fontsize=12, ha="center", va="center", weight="bold", zorder=7)

    def break_line(self, x, y0, y1) -> None:
        n = 40
        pts = [(x + 1.6 * math.sin((i / n) * 2 * math.pi), y0 + (y1 - y0) * i / n) for i in range(n + 1)]
        self.thin(pts, z=4)

    # -- output
    def save(self, pdf: Path) -> None:
        pdf.parent.mkdir(parents=True, exist_ok=True)
        self.fig.savefig(
            pdf, format="pdf", metadata={"Creator": "rfq-agent gen_drawings.py", "CreationDate": None}
        )

    def text_overlaps(self) -> list[tuple[str, str]]:
        """Pairs of text labels whose rendered bounding boxes intersect (legibility check)."""
        r = self.fig.canvas.get_renderer()
        boxes = [(t.get_text(), t.get_window_extent(r)) for t in self.ax.texts]
        bad = []
        for i, (a, ba) in enumerate(boxes):
            for b, bb in boxes[i + 1 :]:
                if (
                    ba.x0 < bb.x1 - 0.5
                    and bb.x0 < ba.x1 - 0.5
                    and ba.y0 < bb.y1 - 0.5
                    and bb.y0 < ba.y1 - 0.5
                ):
                    bad.append((a, b))
        return bad


# ---------------------------------------------------------------- part definition
def feat(fid: str, ftype: FeatureType, text: str, location: str, description: str, **kw) -> Feature:
    return Feature(
        id=fid, type=ftype, description=description, evidence=Evidence(text=text, location=location), **kw
    )


@dataclass
class Part:
    """Single source of truth for one drawing: title block, features, notes and view geometry."""

    pn: str
    rev: str
    title: str
    material: str | None
    customer: str
    drawn_by: str
    drawn_on: str
    envelope: Envelope
    draw: Callable[[Sheet, Part], None]
    features: list[Feature] = field(default_factory=list)
    heat_treatment: str | None = None
    surface_treatment: str | None = None
    notes: list[str] = field(default_factory=list)
    parts_list: list[PartsListItem] = field(default_factory=list)
    data_table: list[str] = field(default_factory=list)  # gear data block
    scale: str = "1:1"
    general_tolerance: str = "ISO 2768-mK"
    default_ra: float | None = 3.2
    # layout variant: "classic" (tuning set) or "alt" (hold-out: other title-block field order / labels,
    # smaller dimension text, other block headings) - same sheet zones either way
    layout: str = "classic"
    notes_heading: str = "TECHNICAL REQUIREMENTS / NOTES"
    data_heading: str = "GEAR DATA"

    def ev(self, fid: str) -> str:
        f = next(f for f in self.features if f.id == fid)
        assert f.evidence is not None
        return f.evidence.text

    @property
    def material_text(self) -> str:
        return self.material or ""

    @property
    def ra_text(self) -> str:
        return f"Ra {self.default_ra:g}" if self.default_ra is not None else "-"

    def title_fields(self) -> dict[str, str]:
        """Title-block field -> printed text (only fields that are filled)."""
        d = {
            "part_number": self.pn,
            "revision": self.rev,
            "title": self.title,
            "material": self.material_text,
            "general_tolerance": self.general_tolerance,
            "default_ra_um": self.ra_text if self.default_ra is not None else "",
            "scale": self.scale,
            "units": "mm",
            "drawn_by": self.drawn_by,
            "date": self.drawn_on,
        }
        return {k: v for k, v in d.items() if v}

    def spec(self) -> DrawingSpec:
        tb = TitleBlock(
            part_number=self.pn,
            revision=self.rev,
            title=self.title,
            material=self.material,
            general_tolerance=self.general_tolerance,
            default_ra_um=self.default_ra,
            scale=self.scale,
            units="mm",
            drawn_by=self.drawn_by,
            date=self.drawn_on,
            evidence={k: Evidence(text=v, location="title block") for k, v in self.title_fields().items()},
        )
        return DrawingSpec(
            drawing_file=f"{self.pn}.pdf",
            title_block=tb,
            envelope=self.envelope,
            features=self.features,
            heat_treatment=self.heat_treatment,
            surface_treatment=self.surface_treatment,
            notes=self.notes,
            parts_list=self.parts_list,
        )


# ---------------------------------------------------------------- right-column blocks
def title_block(s: Sheet, p: Part) -> None:
    x0, y0, x1, y1 = RIGHT_X0, TB_Y0, RIGHT_X1, TB_Y1
    s.rect(x0, y0, x1, y1)
    rows = [  # (y_bottom, [(label, value, width, value_size)])
        (52, [("Company", p.customer, 70, 11), ("Title", p.title, 105, 14)]),
        (
            38,
            [("Part number", p.pn, 55, 14), ("Rev.", p.rev, 20, 14), ("Material", p.material_text, 100, 13)],
        ),
        (
            24,
            [
                ("General tolerance", p.general_tolerance, 50, 12),
                ("Surface", p.ra_text, 35, 12),
                ("Scale", p.scale, 25, 12),
                ("Units", "mm", 25, 12),
                ("Sheet", "1 / 1", 40, 12),
            ],
        ),
        (
            10,
            [
                ("Drawn", p.drawn_by, 45, 12),
                ("Date", p.drawn_on, 40, 12),
                ("Checked", "-", 45, 12),
                ("Format", "A3", 45, 12),
            ],
        ),
    ]
    for yb, cells in rows:
        s.thin([(x0, yb + 14), (x1, yb + 14)])
        x = x0
        for label, value, w, size in cells:
            if x > x0:
                s.thin([(x, yb), (x, yb + 14)])
            s.ax.text(x + 1.5, yb + 12.8, label, fontsize=TB_LABEL_PT, ha="left", va="top", color="#222")
            if value:
                weight = "bold" if label in ("Part number", "Title", "Material", "Rev.") else "normal"
                s.ax.text(x + 1.5, yb + 1.6, value, fontsize=size, ha="left", va="bottom", weight=weight)
            x += w
    s.rect(x0, y0, x1, y1)


def title_block_alt(s: Sheet, p: Part) -> None:
    """Hold-out variant: same zone, other field order and labels, smaller label / value text, no bold title."""
    x0, y0, x1, y1 = RIGHT_X0, TB_Y0, RIGHT_X1, TB_Y1
    rows = [  # top row first
        (
            52,
            [
                ("Drawing No.", p.pn, 60, 12),
                ("Issue", p.rev, 20, 12),
                ("Scale", p.scale, 30, 11),
                ("Unit", "mm", 25, 11),
                ("Sheet", "1 of 1", 40, 11),
            ],
        ),
        (38, [("Description", p.title, 105, 12), ("Customer", p.customer, 70, 9.5)]),
        (
            24,
            [
                ("Material", p.material_text, 80, 11),
                ("Tolerances unless stated", p.general_tolerance, 60, 11),
                ("Finish", p.ra_text, 35, 11),
            ],
        ),
        (
            10,
            [
                ("Drawn", p.drawn_by, 45, 11),
                ("Date", p.drawn_on, 40, 11),
                ("Approved", "-", 45, 11),
                ("Size", "A3", 45, 11),
            ],
        ),
    ]
    for yb, cells in rows:
        s.thin([(x0, yb + 14), (x1, yb + 14)])
        x = x0
        for label, value, w, size in cells:
            if x > x0:
                s.thin([(x, yb), (x, yb + 14)])
            s.ax.text(
                x + 1.2, yb + 13.0, label, fontsize=8.5, ha="left", va="top", color="#333", style="italic"
            )
            if value:
                weight = "bold" if label == "Drawing No." else "normal"
                s.ax.text(x + 1.2, yb + 1.5, value, fontsize=size, ha="left", va="bottom", weight=weight)
            x += w
    s.rect(x0, y0, x1, y1, lw=1.4)


def parts_list_block(s: Sheet, items: list[PartsListItem], y0: float) -> float:
    """Parts list table sitting on y0; returns its top edge."""
    cols = [
        ("Pos.", 11),
        ("Qty", 11),
        ("Part number", 34),
        ("Description", 57),
        ("Standard", 25),
        ("Material", 37),
    ]
    row_h = 8.0
    top = y0 + row_h * (len(items) + 1)
    s.rect(RIGHT_X0, y0, RIGHT_X1, top)
    header_y = top - row_h
    s.rect(RIGHT_X0, header_y, RIGHT_X1, top, fill="#e6e6e6", z=1)
    for i in range(len(items) + 1):
        s.thin([(RIGHT_X0, y0 + i * row_h), (RIGHT_X1, y0 + i * row_h)])
    x = RIGHT_X0
    for name, w in cols:
        if x > RIGHT_X0:
            s.thin([(x, y0), (x, top)])
        s.ax.text(x + 1.3, header_y + row_h / 2, name, fontsize=10, weight="bold", va="center", zorder=6)
        x += w
    for i, it in enumerate(items):
        yc = header_y - row_h * i - row_h / 2
        vals = [str(it.item_no), str(it.quantity), it.part_number or "", it.description, it.standard or ""]
        vals.append(it.material or "")
        x = RIGHT_X0
        for (_, w), v in zip(cols, vals, strict=True):
            s.ax.text(x + 1.3, yc, v, fontsize=10, va="center", zorder=6)
            x += w
    s.ax.text(RIGHT_X0 + 1.3, top + 1.2, "PARTS LIST", fontsize=11, weight="bold", va="bottom")
    return top + 6.5


def lines_block(
    s: Sheet, heading: str, lines: list[str], y0: float, numbered: bool = True, size: float = NOTE_PT
) -> float:
    """Boxed text block (technical requirements / gear data) on y0 in the right column; returns top."""
    wrapped: list[str] = []
    for i, ln in enumerate(lines, 1):
        prefix = f"{i}. " if numbered else ""
        parts = textwrap.wrap(ln, width=64 - len(prefix)) or [""]
        wrapped.append(prefix + parts[0])
        wrapped.extend(" " * len(prefix) * 2 + p for p in parts[1:])
    line_h = 6.2
    height = 11.0 + line_h * len(wrapped) + 2.0
    top = y0 + height
    s.rect(RIGHT_X0, y0, RIGHT_X1, top)
    s.thin([(RIGHT_X0, top - 9.0), (RIGHT_X1, top - 9.0)])
    s.ax.text(RIGHT_X0 + 2, top - 4.5, heading, fontsize=11, weight="bold", va="center")
    for i, ln in enumerate(wrapped):
        y = top - 9.0 - 4.5 - i * line_h
        label, _, value = ln.partition("|")  # "label|value" rows render as two aligned columns
        s.ax.text(RIGHT_X0 + 3, y, label, fontsize=size, va="center")
        if value:
            s.ax.text(RIGHT_X0 + 60, y, value, fontsize=size, va="center")
    return top


def frame(s: Sheet) -> None:
    x0, y0, x1, y1 = BORDER
    s.rect(x0, y0, x1, y1, lw=2.2)
    # centring marks
    for x, y, dx, dy in ((SHEET_W / 2, y0, 0, -5), (SHEET_W / 2, y1, 0, 5), (x0, SHEET_H / 2, -5, 0)):
        s.thin([(x, y), (x + dx, y + dy)])
    s.thin([(x1, SHEET_H / 2), (x1 + 5, SHEET_H / 2)])


def draw_sheet(p: Part) -> Sheet:
    """Frame, right-column blocks and views of one part (not yet saved)."""
    alt = p.layout == "alt"
    s = Sheet(dim_pt=ALT_DIM_PT if alt else DIM_PT)
    frame(s)
    (title_block_alt if alt else title_block)(s, p)
    y = TB_Y1
    if p.parts_list:
        y = parts_list_block(s, p.parts_list, y)
    size = ALT_NOTE_PT if alt else NOTE_PT
    if p.notes:
        y = lines_block(s, p.notes_heading, p.notes, y + BLOCK_GAP, size=size)
    if p.data_table:
        lines_block(s, p.data_heading, p.data_table, y + BLOCK_GAP, numbered=False, size=size)
    p.draw(s, p)
    return s


def render(p: Part, out_dir: Path) -> tuple[Path, Path, Path, list[tuple[str, str]]]:
    """Draw the sheet, write PDF + 150 dpi PNG preview + gold JSON. Returns paths and text overlaps."""
    s = draw_sheet(p)
    overlaps = s.text_overlaps()

    pdf = out_dir / "drawings" / f"{p.pn}.pdf"
    s.save(pdf)
    plt.close(s.fig)
    png = pdf.with_suffix(".png")
    with pymupdf.open(pdf) as doc:
        doc[0].get_pixmap(dpi=PREVIEW_DPI).save(png)
    js = out_dir / "expected" / f"{p.pn}.json"
    js.parent.mkdir(parents=True, exist_ok=True)
    js.write_text(p.spec().model_dump_json(indent=2) + "\n", encoding="utf-8")
    return pdf, png, js, overlaps


# ---------------------------------------------------------------- SH-4711 output shaft
SHAFT_SEGMENTS = [(0.0, 50.0, 16.0), (50.0, 75.0, 17.5), (75.0, 195.0, 20.0), (195.0, 220.0, 17.5)]
SHAFT_BREAK = (90.0, 180.0, 8.0)  # real x hidden between a and b, drawn gap


def draw_shaft(s: Sheet, p: Part) -> None:
    X0, Y0 = 52.0, 214.0
    a, b, gap = SHAFT_BREAK

    def X(x: float) -> float:
        return X0 + (x if x <= a else x - (b - a) + gap)

    def Y(y: float) -> float:
        return Y0 + y

    s.view_label(X(69), Y(56), "FRONT VIEW")
    s.center([(X(-6), Y0), (X(226), Y0)])
    # outline per segment, split at the break
    for x1, x2, r in SHAFT_SEGMENTS:
        pieces = [(x1, x2)] if not (x1 < a and x2 > b) else [(x1, a), (b, x2)]
        for p1, p2 in pieces:
            q1 = p1 + (1.0 if p1 == 0 else 0.0)
            q2 = p2 - (1.0 if p2 == 220 else 0.0)
            s.line([(X(q1), Y(r)), (X(q2), Y(r))])
            s.line([(X(q1), Y(-r)), (X(q2), Y(-r))])
    for (_, x2, r1), (_, _, r2) in zip(SHAFT_SEGMENTS, SHAFT_SEGMENTS[1:], strict=False):
        R = max(r1, r2)
        s.line([(X(x2), Y(-R)), (X(x2), Y(R))])
    # chamfered ends 1x45
    for xe, r, sgn in ((0.0, 16.0, 1), (220.0, 17.5, -1)):
        s.line([(X(xe), Y(-r + 1)), (X(xe), Y(r - 1))])
        s.line([(X(xe), Y(r - 1)), (X(xe + sgn), Y(r))])
        s.line([(X(xe), Y(-r + 1)), (X(xe + sgn), Y(-r))])
        s.thin([(X(xe + sgn), Y(-r)), (X(xe + sgn), Y(r))])
    # break lines
    for xb in (X(a), X(a) + gap):
        s.break_line(xb, Y(-22), Y(22))
    # keyway (on top, seen in depth)
    s.line([(X(5), Y(11)), (X(45), Y(11))])
    s.line([(X(5), Y(11)), (X(5), Y(16))])
    s.line([(X(45), Y(11)), (X(45), Y(16))])
    # M12 centre thread (hidden)
    for r in (5.1, -5.1):
        s.hidden([(X(0), Y(r)), (X(28), Y(r))])
    for r in (6.0, -6.0):
        s.ax.plot([X(0), X(24)], [Y(r), Y(r)], color=INK, lw=0.6, ls=(0, (3, 2)), zorder=3)
    s.hidden([(X(28), Y(5.1)), (X(31), Y(0)), (X(28), Y(-5.1))])
    s.hidden([(X(24), Y(-6)), (X(24), Y(6))])

    # diameters, carried above the part
    s.diam_up(X(47.5), Y(-16), Y(16), Y(36), p.ev("F1"))
    s.diam_up(X(62.5), Y(-17.5), Y(17.5), Y(27), p.ev("F3"))
    s.diam_up(X(84), Y(-20), Y(20), Y(36), p.ev("F4"))
    s.diam_up(X(207.5), Y(-17.5), Y(17.5), Y(27), p.ev("F5"))
    # keyway length
    s.hdim(X(5), X(45), Y(22), "40", ext_from=(Y(16), Y(16)), tx=X(22))
    # roughness on bearing seats
    s.roughness((X(62.5), Y(-17.5)), (X(57), Y(-29)), "Ra 0.8")
    s.roughness((X(207.5), Y(-17.5)), (X(204), Y(-29)), "Ra 0.8", side="left")
    # centre thread + chamfer callouts
    s.leader((X(10), Y(6)), (X(-20), Y(46)), [p.ev("F6")], side="right")
    s.leader((X(219.5), Y(17.2)), (X(225), Y(30)), [p.ev("F7")], side="right")
    # lengths
    s.hdim(X(0), X(50), Y(-43), "50", ext_from=(Y(-15), Y(-16)))
    s.hdim(X(50), X(75), Y(-43), "25", ext_from=(Y(-17.5), Y(-20)))
    s.hdim(X(195), X(220), Y(-43), "25", ext_from=(Y(-20), Y(-17.5)))
    s.hdim(X(0), X(220), Y(-56), "220", ext_from=(Y(-44), Y(-44)))
    # section A-A marker through the keyway
    s.cut_marker(X(30), Y(-24), "A", (-1, 0))
    s.cut_marker(X(30), Y(31), "A", (-1, 0))

    # section A-A (2:1)
    cx, cy, k = 100.0, 68.0, 2.0
    r = 16 * k
    half_w, kb = 5 * k, 11 * k
    ang0 = math.degrees(math.asin(half_w / r))
    arc = [
        (cx + r * math.cos(math.radians(t)), cy + r * math.sin(math.radians(t)))
        for t in [90 + ang0 + i * (360 - 2 * ang0) / 60 for i in range(61)]
    ]
    y_top = math.sqrt(r * r - half_w * half_w)
    poly = [*arc, (cx + half_w, cy + y_top), (cx + half_w, cy + kb), (cx - half_w, cy + kb)]
    s.hatch(poly, "//")
    s.cross((cx, cy), r + 6)
    s.view_label(cx, cy + r + 24, "SECTION A-A (2:1)")
    s.hdim(cx - half_w, cx + half_w, cy + r + 12, p.ev("F2"), ext_from=(cy + y_top, cy + y_top))
    s.vdim(cx - r - 12, cy - r, cy + kb, "27", ext_from=(cx, cx - half_w), side="left", ty=cy - 4)


def shaft() -> Part:
    return Part(
        pn="SH-4711",
        rev="B",
        title="Output Shaft",
        material="42CrMo4+QT",
        customer="Hallvig Fördertechnik GmbH",
        drawn_by="K. Lindqvist",
        drawn_on="2026-08-12",
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=40, length_mm=220),
        draw=draw_shaft,
        features=[
            feat(
                "F1",
                FeatureType.OUTER_DIAMETER,
                "Ø32 h7",
                "front view",
                "Drive journal Ø32 h7",
                nominal_mm=32,
                length_mm=50,
                tolerance=Tolerance(fit="h7"),
            ),
            feat(
                "F2",
                FeatureType.KEYWAY,
                "10 P9",
                "section A-A",
                "Parallel keyway 10 P9 on drive journal, length 40, keyway bottom at 27 from opposite side",
                nominal_mm=10,
                length_mm=40,
                tolerance=Tolerance(fit="P9"),
            ),
            feat(
                "F3",
                FeatureType.OUTER_DIAMETER,
                "Ø35 k6",
                "front view",
                "Bearing seat Ø35 k6, Ra 0.8 (drive side)",
                nominal_mm=35,
                length_mm=25,
                tolerance=Tolerance(fit="k6"),
                ra_um=0.8,
            ),
            feat(
                "F4",
                FeatureType.OUTER_DIAMETER,
                "Ø40",
                "front view",
                "Shaft body Ø40",
                nominal_mm=40,
                length_mm=120,
            ),
            feat(
                "F5",
                FeatureType.OUTER_DIAMETER,
                "Ø35 k6",
                "front view",
                "Bearing seat Ø35 k6, Ra 0.8 (non-drive side)",
                nominal_mm=35,
                length_mm=25,
                tolerance=Tolerance(fit="k6"),
                ra_um=0.8,
            ),
            feat(
                "F6",
                FeatureType.THREAD,
                "M12 depth 24",
                "front view",
                "Tapped centre hole M12 in drive-end face, thread depth 24",
                nominal_mm=12,
                length_mm=24,
                thread_spec="M12x1.75",
            ),
            feat(
                "F7",
                FeatureType.CHAMFER,
                "2× 1×45°",
                "front view",
                "Chamfer 1x45° at both shaft ends",
                nominal_mm=1,
                quantity=2,
            ),
        ],
        heat_treatment="QT 28-32 HRC",
        notes=[
            "Heat treatment: QT 28-32 HRC.",
            "Bearing seats Ø35 k6: runout 0.01 to common axis.",
            "Break all sharp edges 0.2-0.5.",
            "Centre holes DIN 332-D permitted.",
        ],
    )


# ---------------------------------------------------------------- FL-2208 motor flange
def draw_flange(s: Sheet, p: Part) -> None:
    cx, cy = 84.0, 168.0
    # front view (recess side)
    s.view_label(cx, cy + 94, "FRONT VIEW")
    s.circle((cx, cy), 60)
    s.circle((cx, cy), 59)  # chamfer edge
    s.circle((cx, cy), 40)
    s.circle((cx, cy), 26)
    s.center_circle((cx, cy), 50)
    s.cross((cx, cy), 66)
    for ang in (0, 90, 180, 270):
        c = (cx + 50 * math.cos(math.radians(ang)), cy + 50 * math.sin(math.radians(ang)))
        s.thread_end(c, 3.0)
    for ang in (45, 135, 225, 315):
        c = (cx + 50 * math.cos(math.radians(ang)), cy + 50 * math.sin(math.radians(ang)))
        s.circle(c, 4.5)
        s.cross(c, 6.5)
    s.cut_marker(cx, cy + 69, "A", (1, 0))
    s.cut_marker(cx, cy - 69, "A", (1, 0))
    top = (cx - 1.6, cy + 52.4)
    s.leader(top, (26, 240), [p.ev("F4"), "thread depth 12"], side="right")
    h = (cx + 50 * math.cos(math.radians(225)) - 3.2, cy + 50 * math.sin(math.radians(225)) - 3.2)
    s.leader(h, (32, 92), [p.ev("F5")], side="right")
    od = (cx + 60 * math.cos(math.radians(290)), cy + 60 * math.sin(math.radians(290)))
    s.leader(od, (112, 92), [p.ev("F1")], side="right")

    # section A-A
    sx = 178.0
    s.view_label(sx + 12.5, cy + 94, "SECTION A-A")
    up = [
        (sx + 4, cy + 26),
        (sx + 25, cy + 26),
        (sx + 25, cy + 59),
        (sx + 24, cy + 60),
        (sx + 1, cy + 60),
        (sx, cy + 59),
        (sx, cy + 53),
        (sx + 12, cy + 53),
        (sx + 12, cy + 47),
        (sx, cy + 47),
        (sx, cy + 40),
        (sx + 4, cy + 40),
    ]
    s.hatch(up, "//")
    s.hatch([(x, 2 * cy - y) for x, y in up], "//")
    for sgn in (1, -1):  # tapped hole: thin major-diameter lines, drill point
        s.thin([(sx, cy + sgn * 47), (sx + 12, cy + sgn * 47)])
        s.center([(sx - 4, cy + sgn * 50), (sx + 17, cy + sgn * 50)])
    s.center([(sx - 6, cy), (sx + 31, cy)])
    s.line([(sx + 4, cy - 26), (sx + 4, cy + 26)])
    s.line([(sx, cy - 40), (sx, cy + 40)])
    s.line([(sx + 25, cy - 26), (sx + 25, cy + 26)])

    s.hdim(sx, sx + 25, cy + 72, p.ev("F6"), ext_from=(cy + 60, cy + 60))
    s.vdim(sx - 8, cy - 40, cy + 40, p.ev("F2"), ext_from=(sx, sx), side="left", ty=cy + 8)
    s.vdim(sx + 12, cy - 26, cy + 26, p.ev("F3"), side="right", ty=cy - 6)
    s.hdim(sx, sx + 4, cy + 18, "4", ext_from=(cy + 40, cy + 26), tx=sx - 3.2)
    s.roughness((sx + 4, cy + 33), (sx - 12, cy + 58), "Ra 1.6", side="left")
    s.leader((sx + 24.6, cy - 59.6), (sx + 32, cy - 72), [p.ev("F7")], side="right")


def flange() -> Part:
    return Part(
        pn="FL-2208",
        rev="A",
        title="Motor Flange",
        material="EN AW-6082 T6",
        customer="Norvane Motion Systems Ltd",
        drawn_by="R. Ashdown",
        drawn_on="2026-07-29",
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=120, length_mm=25),
        draw=draw_flange,
        features=[
            feat("F1", FeatureType.OUTER_DIAMETER, "Ø120", "front view", "Flange outer diameter Ø120",
                 nominal_mm=120, length_mm=25),
            feat("F2", FeatureType.BORE, "Ø80 H7", "section A-A",
                 "Centring recess Ø80 H7, depth 4, Ra 1.6", nominal_mm=80, length_mm=4,
                 tolerance=Tolerance(fit="H7"), ra_um=1.6),
            feat("F3", FeatureType.BORE, "Ø52", "section A-A", "Through bore Ø52",
                 nominal_mm=52, length_mm=21),
            feat("F4", FeatureType.THREAD, "4× M6 on PCD 100", "front view",
                 "4x tapped hole M6 on PCD 100, thread depth 12", nominal_mm=6, length_mm=12,
                 quantity=4, thread_spec="M6x1"),
            feat("F5", FeatureType.HOLE, "4× Ø9 THRU on PCD 100", "front view",
                 "4x through hole Ø9 on PCD 100", nominal_mm=9, length_mm=25, quantity=4),
            feat("F6", FeatureType.FACE, "25 ±0.1", "section A-A", "Flange thickness 25 ±0.1",
                 nominal_mm=25, tolerance=Tolerance(upper=0.1, lower=-0.1)),
            feat("F7", FeatureType.CHAMFER, "1×45°", "section A-A", "Chamfer 1x45° on outer edges",
                 nominal_mm=1),
        ],
        surface_treatment="anodized black",
        notes=[
            "Surface treatment: anodized black, layer 15-20 µm.",
            "Recess Ø80 H7 and mating face: no anodizing damage permitted.",
            "Break all sharp edges 0.2-0.5.",
        ],
    )  # fmt: skip


# ---------------------------------------------------------------- BR-0930 mounting bracket
def draw_bracket(s: Sheet, p: Part) -> None:
    X0 = 38.0
    FY = 178.0  # front view: bottom face (z=0)
    TY = 62.0  # top view: front face (y=0)

    # front view
    s.view_label(X0 + 80, FY + 84, "FRONT VIEW")
    s.rect(X0, FY, X0 + 160, FY + 60)
    s.hidden([(X0 + 20, FY + 60), (X0 + 20, FY + 20), (X0 + 140, FY + 20), (X0 + 140, FY + 60)])
    for dz in (37.0, 43.0):  # deep hole at z=40 in the back wall
        s.hidden([(X0 + 80, FY + dz), (X0 + 160, FY + dz)])
    s.hidden([(X0 + 80, FY + 37), (X0 + 76.4, FY + 40), (X0 + 80, FY + 43)])
    s.center([(X0 + 72, FY + 40), (X0 + 166, FY + 40)])
    s.vdim(X0 - 10, FY, FY + 60, "60", ext_from=(X0, X0), side="left")
    s.hdim(X0, X0 + 160, FY + 71, "160", ext_from=(FY + 60, FY + 60))
    s.leader((X0 + 150, FY + 43.2), (X0 + 166, FY + 52), [p.ev("F3")], side="right")

    # top view
    s.view_label(X0 + 80, TY - 30, "TOP VIEW")
    s.rect(X0, TY, X0 + 160, TY + 80)
    r = 6.0
    px0, px1, py0, py1 = X0 + 20, X0 + 140, TY + 1.5, TY + 63.5
    s.line([(px0 + r, py0), (px1 - r, py0)])
    s.line([(px0 + r, py1), (px1 - r, py1)])
    s.line([(px0, py0 + r), (px0, py1 - r)])
    s.line([(px1, py0 + r), (px1, py1 - r)])
    for (ccx, ccy), t1 in (
        ((px0 + r, py0 + r), 180),
        ((px1 - r, py0 + r), 270),
        ((px1 - r, py1 - r), 0),
        ((px0 + r, py1 - r), 90),
    ):
        s.ax.add_patch(Arc((ccx, ccy), 2 * r, 2 * r, theta1=t1, theta2=t1 + 90, lw=LW_THICK, zorder=3))
    for x, y in ((15, 15), (145, 15), (15, 72), (145, 72)):
        s.ax.add_patch(Circle((X0 + x, TY + y), 4, fill=False, lw=1.0, ls=(0, (3, 2)), zorder=3))
        s.cross((X0 + x, TY + y), 6)
    for x in (10, 150):
        s.circle((X0 + x, TY + 40), 4)
        s.cross((X0 + x, TY + 40), 6)
    for dy in (69.0, 75.0):
        s.hidden([(X0 + 80, TY + dy), (X0 + 160, TY + dy)])
    s.vdim(X0 + 172, TY, TY + 80, "80", ext_from=(X0 + 160, X0 + 160), side="right")
    s.leader((X0 + 60, TY + 0.75), (X0 + 44, TY - 14), [p.ev("F2")], side="left")
    s.leader((X0 + 100, py1), (X0 + 108, TY + 94), [p.ev("F1")], side="right")
    s.leader((X0 + 12.2, TY + 74.8), (X0 + 22, TY + 94), [p.ev("F4")], side="left")
    s.leader((X0 + 152.8, TY + 37.2), (X0 + 120, TY - 14), [p.ev("F5")], side="right")


def bracket() -> Part:
    return Part(
        pn="BR-0930",
        rev="A",
        title="Mounting Bracket",
        material=None,  # deliberately blank -> VAL-001
        customer="Tervalo Automation B.V.",
        drawn_by="S. Verhoek",
        drawn_on="2026-09-02",
        envelope=Envelope(shape_class=ShapeClass.PRISMATIC, length_mm=160, width_mm=80, height_mm=60),
        draw=draw_bracket,
        features=[
            feat("F1", FeatureType.POCKET, "POCKET 120×62 depth 40, R6", "top view",
                 "Pocket 120 x 62, depth 40, corner radius R6", nominal_mm=62, length_mm=120),
            feat("F2", FeatureType.WALL, "WALL 1.5", "top view",
                 "Thin side wall 1.5 between pocket and front face, height 40", nominal_mm=1.5,
                 length_mm=120),
            feat("F3", FeatureType.HOLE, "Ø6 depth 80", "front view",
                 "Deep blind hole Ø6, depth 80, drilled from right end face", nominal_mm=6, length_mm=80),
            feat("F4", FeatureType.THREAD, "4× M8 depth 16", "top view",
                 "4x tapped hole M8 in bottom face, thread depth 16", nominal_mm=8, length_mm=16,
                 quantity=4, thread_spec="M8x1.25"),
            feat("F5", FeatureType.HOLE, "2× Ø8 H7 depth 12", "top view",
                 "2x dowel hole Ø8 H7, depth 12, in top face", nominal_mm=8, length_mm=12, quantity=2,
                 tolerance=Tolerance(fit="H7")),
        ],
        notes=[
            "Pocket corners R6 unless otherwise stated.",
            "Break all sharp edges 0.3×45°.",
            "Surfaces free of burrs and scratches.",
        ],
    )  # fmt: skip


# ---------------------------------------------------------------- ASM-5100 actuator sub-assembly
def draw_assembly(s: Sheet, p: Part) -> None:
    X0, Y0 = 72.0, 192.0

    def P(x: float, y: float) -> tuple[float, float]:
        return (X0 + x, Y0 + y)

    s.view_label(*P(30, 88), "SECTION A-A")
    s.center([P(-36, 0), P(100, 0)])
    # housing (1)
    hs = [(0, 17.5), (7, 17.5), (7, 23.5), (80, 23.5), (80, 45), (0, 45)]
    s.hatch([P(x, y) for x, y in hs], "//")
    s.hatch([P(x, -y) for x, y in hs], "//")
    # end cover (6)
    cov = [(80, -45), (88, -45), (88, 45), (80, 45), (80, 23.5), (74, 23.5), (74, 19), (80, 19),
           (80, -19), (74, -19), (74, -23.5), (80, -23.5)]  # fmt: skip
    s.hatch([P(x, y) for x, y in cov], "\\\\")
    # shaft (2) - not sectioned
    s.rect(*P(-30, -10), *P(76, 10), fill="white", z=3)
    s.thin([P(-29, -10), P(-29, 10)], z=4)
    # bearings (3): simplified representation
    for x0 in (7.0, 60.0):
        for sgn in (1, -1):
            y0, y1 = sorted((sgn * 10.0, sgn * 23.5))
            s.rect(*P(x0, y0), *P(x0 + 14, y1), fill="white", z=4)
            s.thin([P(x0, y0), P(x0 + 14, y1)], z=4)
            s.thin([P(x0, y1), P(x0 + 14, y0)], z=4)
    # shaft seal (4)
    for sgn in (1, -1):
        y0, y1 = sorted((sgn * 10.0, sgn * 17.5))
        s.rect(*P(0, y0), *P(7, y1), fill="#bdbdbd", z=4)
    # retaining ring (7)
    for sgn in (1, -1):
        y0, y1 = sorted((sgn * 9.0, sgn * 12.0))
        s.rect(*P(74, y0), *P(75.4, y1), fill=INK, z=5)
    # screws (5)
    for sgn in (1, -1):
        s.rect(*P(72, sgn * 35 - 2.5), *P(88, sgn * 35 + 2.5), lw=1.2, fill="white", z=4)
        s.rect(*P(88, sgn * 35 - 4.25), *P(93, sgn * 35 + 4.25), lw=1.2, fill="white", z=4)
        s.center([P(68, sgn * 35), P(97, sgn * 35)])

    balloons = [  # (item_no, target, balloon centre)
        (2, (-20, 10), (-24, 64)),
        (4, (3, 15), (-6, 64)),
        (3, (14, 20), (12, 64)),
        (1, (40, 38), (34, 64)),
        (7, (74.7, 11.5), (58, 64)),
        (6, (84, 30), (80, 64)),
        (5, (91, 39.3), (100, 64)),
    ]
    for n, t, c in balloons:
        s.balloon(P(*t), P(*c), n)
    s.vdim(X0 + 104, Y0 - 45, Y0 + 45, "Ø90", ext_from=(X0 + 88, X0 + 88), side="right", ty=Y0 - 14)
    s.hdim(X0 - 30, X0 + 93, Y0 - 58, "123", ext_from=(Y0 - 10, Y0 - 40))
    s.cut_marker(X0 + 112, Y0 + 34, "Z", (-1, 0))

    # view Z (end cover side)
    c = (X0 + 28.0, 66.0)
    s.view_label(c[0], c[1] + 56, "VIEW Z")
    s.cut_marker(c[0], c[1] + 49, "A", (1, 0))
    s.cut_marker(c[0], c[1] - 49, "A", (1, 0))
    s.circle(c, 45)
    s.circle(c, 44)
    s.center_circle(c, 35)
    s.cross(c, 51)
    for ang in (0, 90, 180, 270):
        hc = (c[0] + 35 * math.cos(math.radians(ang)), c[1] + 35 * math.sin(math.radians(ang)))
        s.circle(hc, 4.25, lw=1.2)
        hexagon = [
            (hc[0] + 2 * math.cos(math.radians(a)), hc[1] + 2 * math.sin(math.radians(a)))
            for a in range(0, 360, 60)
        ]
        s.ax.add_patch(Polygon(hexagon, closed=True, fill=False, lw=0.8, zorder=4))


ASM_PARTS = [
    PartsListItem(item_no=1, part_number="HS-5101", description="Gehäuse", quantity=1, material="EN-GJS-500-7"),
    PartsListItem(item_no=2, part_number="SH-5102", description="Welle", quantity=1, material="C45"),
    PartsListItem(item_no=3, part_number="6204-2RS", description="Rillenkugellager 20×47×14", quantity=2,
                  standard="DIN 625"),
    PartsListItem(item_no=4, description="Wellendichtring 20×35×7 NBR", quantity=1, standard="DIN 3760"),
    PartsListItem(item_no=5, description="Zylinderschraube M5×16-8.8", quantity=4, standard="ISO 4762"),
    PartsListItem(item_no=6, part_number="EC-5103", description="Enddeckel", quantity=1,
                  material="EN AW-6082 T6"),
    PartsListItem(item_no=7, description="Sicherungsring 20×1.2", quantity=1, standard="DIN 471"),
]  # fmt: skip


def assembly() -> Part:
    return Part(
        pn="ASM-5100",
        rev="C",
        title="Actuator Sub-Assembly",
        material="see parts list",
        customer="Veltrum Aktorik GmbH",
        drawn_by="M. Hollerbeck",
        drawn_on="2026-09-08",
        envelope=Envelope(shape_class=ShapeClass.ASSEMBLY, max_diameter_mm=90, length_mm=123),
        draw=draw_assembly,
        default_ra=None,
        parts_list=ASM_PARTS,
        notes=[
            "Tightening torque pos. 5: 6 Nm, secure with medium-strength threadlocker.",
            "Bearings pos. 3 pre-greased; fill seal lip pos. 4 with grease before assembly.",
            "Shaft must rotate freely by hand after assembly; 100 % functional test.",
        ],
    )


# ---------------------------------------------------------------- GR-3340 spur gear (eval only)
def draw_gear(s: Sheet, p: Part) -> None:
    cx, cy = 88.0, 165.0
    s.view_label(cx, cy + 66, "FRONT VIEW")
    s.circle((cx, cy), 42)
    s.circle((cx, cy), 41)
    s.circle((cx, cy), 37.5, lw=LW_THIN)
    s.center_circle((cx, cy), 40)
    s.cross((cx, cy), 48)
    kt = 15.8
    ang0 = math.degrees(math.asin(4 / 12.5))
    bore = [
        (cx + 12.5 * math.cos(math.radians(t)), cy + 12.5 * math.sin(math.radians(t)))
        for t in [90 + ang0 + i * (360 - 2 * ang0) / 60 for i in range(61)]
    ]
    s.line([*bore, (cx + 4, cy + 12.5 * math.cos(math.radians(ang0))), (cx + 4, cy + kt), (cx - 4, cy + kt),
            (cx - 4, cy + 12.5 * math.cos(math.radians(ang0)))])  # fmt: skip
    s.hdim(cx - 4, cx + 4, cy + 24, p.ev("F4"), ext_from=(cy + kt, cy + kt), tx=cx + 13)
    s.vdim(cx - 22, cy - 12.5, cy + kt, "28.3", ext_from=(cx, cx - 4), side="left", ty=cy + 2)
    b = (cx + 12.5 * math.cos(math.radians(315)), cy + 12.5 * math.sin(math.radians(315)))
    s.leader(b, (cx + 30, cy - 62), [p.ev("F3")], side="right")
    b2 = (cx + 12.5 * math.cos(math.radians(235)), cy + 12.5 * math.sin(math.radians(235)))
    s.roughness(b2, (cx - 24, cy - 62), "Ra 1.6", side="left")
    s.cut_marker(cx, cy + 51, "A", (1, 0))
    s.cut_marker(cx, cy - 51, "A", (1, 0))

    # section A-A
    sx = 160.0
    s.view_label(sx + 10, cy + 66, "SECTION A-A")
    up = [(sx, cy + kt), (sx + 20, cy + kt), (sx + 20, cy + 41), (sx + 19, cy + 42), (sx + 1, cy + 42),
          (sx, cy + 41)]  # fmt: skip
    lo = [(sx, cy - 12.5), (sx + 20, cy - 12.5), (sx + 20, cy - 41), (sx + 19, cy - 42), (sx + 1, cy - 42),
          (sx, cy - 41)]  # fmt: skip
    s.hatch(up, "//")
    s.hatch(lo, "//")
    for sgn in (1, -1):
        s.thin([(sx, cy + sgn * 37.5), (sx + 20, cy + sgn * 37.5)], z=4)
        s.center([(sx - 4, cy + sgn * 40), (sx + 24, cy + sgn * 40)])
    s.center([(sx - 6, cy), (sx + 26, cy)])
    s.thin([(sx, cy - 12.5), (sx, cy + kt)])
    s.thin([(sx + 20, cy - 12.5), (sx + 20, cy + kt)])
    s.hdim(sx, sx + 20, cy + 52, "20", ext_from=(cy + 42, cy + 42))
    s.vdim(sx + 34, cy - 42, cy + 42, p.ev("F2"), ext_from=(sx + 20, sx + 20), side="right", ty=cy - 8)
    s.leader((sx + 19.6, cy - 41.6), (sx + 26, cy - 60), [p.ev("F5")], side="right")


def gear() -> Part:
    return Part(
        pn="GR-3340",
        rev="A",
        title="Spur Gear",
        material="16MnCr5",
        customer="Kartano Getriebebau GmbH",
        drawn_by="P. Wendholt",
        drawn_on="2026-06-17",
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=84, length_mm=20),
        draw=draw_gear,
        features=[
            feat("F1", FeatureType.GEAR_TEETH, "m = 2", "gear data",
                 "Spur gear teeth m 2, z 40, pressure angle 20°, pitch diameter 80, face width 20",
                 nominal_mm=80, gear_module=2, gear_teeth=40, face_width_mm=20),
            feat("F2", FeatureType.OUTER_DIAMETER, "Ø84 h11", "section A-A", "Tip diameter Ø84 h11",
                 nominal_mm=84, length_mm=20, tolerance=Tolerance(fit="h11")),
            feat("F3", FeatureType.BORE, "Ø25 H7", "front view", "Bore Ø25 H7, Ra 1.6",
                 nominal_mm=25, length_mm=20, tolerance=Tolerance(fit="H7"), ra_um=1.6),
            feat("F4", FeatureType.KEYWAY, "8 JS9", "front view",
                 "Hub keyway 8 JS9 through, depth 28.3 from bore bottom", nominal_mm=8, length_mm=20,
                 tolerance=Tolerance(fit="JS9")),
            feat("F5", FeatureType.CHAMFER, "2× 1×45°", "section A-A", "Chamfer 1x45° on tip edges",
                 nominal_mm=1, quantity=2),
        ],
        heat_treatment="case hardened 58-62 HRC",
        notes=[
            "Heat treatment: case hardened 58-62 HRC, CHD 0.6-0.9 mm.",
            "Bore and faces ground after hardening.",
            "Break all sharp edges 0.2-0.5.",
        ],
        data_table=[
            "Module|m = 2",
            "Number of teeth|z = 40",
            "Pressure angle|α = 20°",
            "Pitch diameter|d = 80",
            "Face width|b = 20",
            "Accuracy|ISO 1328-1 grade 7",
        ],
    )  # fmt: skip


# ================================================================ HOLD-OUT SET
# Three parts created AFTER the prompts and the deterministic repairs were frozen, and never used to tune them.
# They differ on purpose from the tuning set: other part families (sleeve with circlip groove and cross hole,
# plate with counterbores / slot / tapped PCD, pinion shaft with spline + integral teeth + centre holes), other
# materials, the "alt" title block (field order, labels, smaller text), 2:1 sheet scale on the bushing, a plate
# drawn as top view + section, and callout styles that appear nowhere in the prompts.
HOLDOUT_DIR = SAMPLES_DIR / "holdout"


# ---------------------------------------------------------------- BS-60218 flanged guide bushing (2:1)
def draw_bushing(s: Sheet, p: Part) -> None:
    k = 2.0  # sheet scale 2:1
    X0, Y0 = 54.0, 128.0  # section A-A: left end (collar side), axis

    def X(x: float) -> float:
        return X0 + k * x

    def Y(y: float) -> float:
        return Y0 + k * y

    L, lc = 56.0, 10.0  # overall length, collar length
    rc, ro, rb = 28.0, 22.5, 15.0  # collar, body, bore radius
    g0, g1, rg = 49.5, 50.7, 15.7  # circlip groove (DIN 472, bore 30)
    xh, rh = 33.0, 2.5  # radial lube hole
    ch = 1.5  # lead-in chamfer 1.5 x 30 deg on the press-fit end (axial 1.5, radial 1.5*tan30)
    cr = ch * math.tan(math.radians(30))

    s.view_label(X(22), Y(rc) + 50, "SECTION A-A")
    s.center([(X(-4), Y0), (X(L + 4), Y0)])
    for sgn in (1, -1):
        # wall outline (hatched), split at the lube hole on the upper side and at the groove
        outer = [(0, rc), (lc, rc), (lc, ro), (L - ch, ro), (L, ro - cr)]
        if sgn == 1:
            left = [(0, rb), *outer[:3], (xh - rh, ro), (xh - rh, rb)]
            right = [
                (xh + rh, rb),
                (xh + rh, ro),
                *outer[3:],
                (L, rb),
                (g1, rb),
                (g1, rg),
                (g0, rg),
                (g0, rb),
            ]
            for poly in (left, right):
                s.hatch([(X(x), Y(y)) for x, y in poly], "//")
        else:
            poly = [(0, rb), *outer, (L, rb), (g1, rb), (g1, rg), (g0, rg), (g0, rb)]
            s.hatch([(X(x), Y(-y)) for x, y in poly], "//")
    s.center([(X(xh), Y(rb) - 3), (X(xh), Y(ro) + 3)])

    # diameters
    s.vdim(X0 - 10, Y(-rc), Y(rc), p.ev("F1"), ext_from=(X0, X0), side="left", ty=Y0 + 20)
    s.vdim(X(L) + 14, Y(-ro), Y(ro), p.ev("F2"), ext_from=(X(L), X(L)), side="right", ty=Y0 + 22)
    s.roughness((X(47), Y(ro)), (X(50), Y(ro) + 9), "Ra 1.6", side="right")
    s.leader((X(20), Y(rb)), (X(12), Y(rc) + 16), [p.ev("F3")], side="left")
    s.roughness((X(24), Y(-rb)), (X(18), Y(-rb) + 16), "Ra 0.2", side="right")
    s.leader((X((g0 + g1) / 2), Y(rg)), (X(44), Y(rc) + 30), [p.ev("F4")], side="left")
    s.leader((X(xh + rh), Y(ro)), (X(38), Y(rc) + 20), [p.ev("F5")], side="right")
    s.leader((X(L) - 0.5, Y(-ro) + 0.8), (X(L) + 6, Y(-rc) - 12), [p.ev("F6")], side="right")
    # lengths
    s.hdim(X(0), X(lc), Y(-rc) - 12, "10", ext_from=(Y(-rc), Y(-rc)))
    s.hdim(X(0), X(xh), Y(-rc) - 24, "33", ext_from=(Y(-rc), Y(-ro)))
    s.hdim(X(0), X(L), Y(-rc) - 36, p.ev("F7"), ext_from=(Y(-rc) - 12, Y(-ro)))
    s.hdim(X(g0), X(L), Y(-ro) - 6, "6.5", ext_from=(Y(-rg), Y(-ro)), tx=X(g0) - 12)

    # view X (1:1) from the collar side
    c = (182.0, 236.0)
    s.view_label(c[0], c[1] + 40, "VIEW X (1:1)")
    s.circle(c, rc)
    s.circle(c, rb)
    s.ax.add_patch(Circle(c, rg, fill=False, lw=1.0, ls=(0, (3, 2)), zorder=3))
    s.cross(c, rc + 5)
    s.cut_marker(c[0] - rc - 9, c[1], "A", (0, -1))
    s.cut_marker(c[0] + rc + 9, c[1], "A", (0, -1))


def bushing() -> Part:
    return Part(
        pn="BS-60218",
        rev="02",
        title="Flanged Guide Bushing",
        material="X5CrNi18-10 (1.4301)",
        customer="Brenntal Hydraulik AG",
        drawn_by="T. Aaltonen",
        drawn_on="03.07.2026",
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=56, length_mm=56),
        draw=draw_bushing,
        scale="2:1",
        general_tolerance="ISO 2768-fK",
        layout="alt",
        notes_heading="NOTES",
        features=[
            feat("F1", FeatureType.OUTER_DIAMETER, "Ø56", "section A-A", "Collar Ø56, length 10",
                 nominal_mm=56, length_mm=10),
            feat("F2", FeatureType.OUTER_DIAMETER, "Ø45 r6", "section A-A",
                 "Press-fit diameter Ø45 r6, Ra 1.6", nominal_mm=45, length_mm=46,
                 tolerance=Tolerance(fit="r6"), ra_um=1.6),
            feat("F3", FeatureType.BORE, "Ø30 H7", "section A-A", "Guide bore Ø30 H7 through, Ra 0.2",
                 nominal_mm=30, length_mm=56, tolerance=Tolerance(fit="H7"), ra_um=0.2),
            feat("F4", FeatureType.GROOVE, "GROOVE Ø31.4×1.2 DIN 472", "section A-A",
                 "Internal circlip groove Ø31.4 x 1.2 (DIN 472), 6.5 from the press-fit end",
                 nominal_mm=31.4, length_mm=1.2),
            feat("F5", FeatureType.HOLE, "Ø5 THRU", "section A-A",
                 "Radial lubrication hole Ø5 through the wall, 33 from the collar face", nominal_mm=5),
            feat("F6", FeatureType.CHAMFER, "1.5×30°", "section A-A",
                 "Press-in lead chamfer 1.5 x 30° on Ø45", nominal_mm=1.5),
            feat("F7", FeatureType.FACE, "56 ±0.05", "section A-A", "Overall length 56 ±0.05",
                 nominal_mm=56, tolerance=Tolerance(upper=0.05, lower=-0.05)),
        ],
        surface_treatment="passivated",
        notes=[
            "Surface treatment: passivated, free of iron contamination.",
            "Bore Ø30 H7: cylindricity 0.005, honed.",
            "Ø45 r6 coaxial to bore within 0.02.",
            "Deburr the cross hole inside the bore.",
        ],
    )  # fmt: skip


# ---------------------------------------------------------------- CP-7390 adapter plate (top view + section)
PLATE_L, PLATE_W, PLATE_T = 150.0, 100.0, 20.0
PLATE_BORE = (65.0, 50.0, 28.0)  # centre bore u, v, r
PLATE_CBORE = [(12.0, 12.0), (138.0, 12.0), (12.0, 88.0), (138.0, 88.0)]
PLATE_DOWELS = [(12.0, 50.0), (110.0, 50.0)]
PLATE_SLOT = (132.0, 36.0, 64.0, 6.0)  # u, v of the two end centres, half width


def draw_plate(s: Sheet, p: Part) -> None:
    X0, TY = 52.0, 146.0  # top view: plate corner (u=0, v=0)
    SY = 70.0  # section A-A: bottom face

    def T(u: float, v: float) -> tuple[float, float]:
        return (X0 + u, TY + v)

    bu, bv, br = PLATE_BORE
    s.view_label(X0 + 75, TY + PLATE_W + 34, "TOP VIEW")
    s.rect(*T(0, 0), *T(PLATE_L, PLATE_W))
    s.circle(T(bu, bv), br)
    s.cross(T(bu, bv), br + 6)
    s.center_circle(T(bu, bv), 36)
    for ang in range(30, 360, 60):
        c = T(bu + 36 * math.cos(math.radians(ang)), bv + 36 * math.sin(math.radians(ang)))
        s.thread_end(c, 3.0)
        s.cross(c, 4.5)
    for u, v in PLATE_CBORE:
        s.circle(T(u, v), 6.75)
        s.circle(T(u, v), 10.0, lw=1.2)
        s.cross(T(u, v), 12)
    for u, v in PLATE_DOWELS:
        s.circle(T(u, v), 5.0)
        s.cross(T(u, v), 7)
    su, sv0, sv1, sr = PLATE_SLOT
    s.line([T(su - sr, sv0), T(su - sr, sv1)])
    s.line([T(su + sr, sv0), T(su + sr, sv1)])
    for vc, t1 in ((sv0, 180), (sv1, 0)):
        s.ax.add_patch(Arc(T(su, vc), 2 * sr, 2 * sr, theta1=t1, theta2=t1 + 180, lw=LW_THICK, zorder=3))
    s.center([T(su, sv0 - 9), T(su, sv1 + 9)])
    s.cut_marker(X0 - 8, TY + bv, "A", (0, -1))
    s.cut_marker(X0 + PLATE_L + 8, TY + bv, "A", (0, -1))

    # callouts
    s.leader(T(12 + 7.1, 88 + 7.1), T(34, 112), [p.ev("F1"), p.ev("F2")], side="right")
    top_m6 = T(bu + 36 * math.cos(math.radians(90)) + 2.2, bv + 36 + 2.2)
    s.leader(top_m6, T(92, 122), [p.ev("F3")], side="right")
    s.leader(T(bu - br * 0.707, bv - br * 0.707), T(18, -34), [p.ev("F4")], side="left")
    d = PLATE_DOWELS[1]
    s.leader(T(d[0] + 3.5, d[1] - 3.5), T(84, -34), [p.ev("F5")], side="right")
    s.leader(T(su, sv1 + sr), T(118, 112), [p.ev("F6")], side="right")
    s.hdim(X0, X0 + PLATE_L, TY - 20, "150", ext_from=(TY, TY))
    s.vdim(X0 - 16, TY, TY + PLATE_W, "100", ext_from=(X0, X0), side="left", ty=TY + 24)
    s.hdim(X0, X0 + bu, TY - 8, "65", ext_from=(TY, TY), tx=X0 + 40)

    # section A-A through the bore, dowels and slot (v = 50)
    s.view_label(X0 + 75, SY + PLATE_T + 10, "SECTION A-A")
    cuts = sorted([(u - 5, u + 5) for u, _ in PLATE_DOWELS] + [(bu - br, bu + br), (su - sr, su + sr)])
    edges = [0.0] + [x for c in cuts for x in c] + [PLATE_L]
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        s.hatch([(X0 + a, SY), (X0 + b, SY), (X0 + b, SY + PLATE_T), (X0 + a, SY + PLATE_T)], "//")
    for a, b in cuts:
        s.center([(X0 + (a + b) / 2, SY - 4), (X0 + (a + b) / 2, SY + PLATE_T + 4)])
    s.vdim(X0 + PLATE_L + 10, SY, SY + PLATE_T, "20", ext_from=(X0 + PLATE_L, X0 + PLATE_L), side="right")
    s.hdim(X0 + bu - br, X0 + bu + br, SY - 12, "Ø56", ext_from=(SY, SY))


def plate() -> Part:
    return Part(
        pn="CP-7390",
        rev="1",
        title="Adapter Plate",
        material="EN-GJS-500-7",
        customer="Ostvik Marine Drives AB",
        drawn_by="B. Kowalczyk",
        drawn_on="21.08.2026",
        envelope=Envelope(shape_class=ShapeClass.PRISMATIC, length_mm=150, width_mm=100, height_mm=20),
        draw=draw_plate,
        general_tolerance="ISO 2768-mH",
        layout="alt",
        notes_heading="NOTES",
        features=[
            feat("F1", FeatureType.HOLE, "4× Ø13.5 THRU", "top view",
                 "4x through hole Ø13.5 for M12 cap screws", nominal_mm=13.5, length_mm=20, quantity=4),
            feat("F2", FeatureType.HOLE, "C'BORE Ø20 depth 13", "top view",
                 "4x counterbore Ø20, depth 13, on the through holes", nominal_mm=20, length_mm=13,
                 quantity=4),
            feat("F3", FeatureType.THREAD, "6× M6-6H depth 9 on PCD 72", "top view",
                 "6x tapped hole M6 on PCD 72, thread depth 9", nominal_mm=6, length_mm=9, quantity=6,
                 thread_spec="M6"),
            feat("F4", FeatureType.BORE, "Ø56 H8", "top view",
                 "Centre register bore Ø56 H8 through (bored seat)", nominal_mm=56, length_mm=20,
                 tolerance=Tolerance(fit="H8")),
            feat("F5", FeatureType.HOLE, "2× Ø10 H7 THRU", "top view", "2x dowel hole Ø10 H7 through",
                 nominal_mm=10, length_mm=20, quantity=2, tolerance=Tolerance(fit="H7")),
            feat("F6", FeatureType.SLOT, "SLOT 40×12 H9 THRU", "top view",
                 "Through slot 40 x 12, width H9", nominal_mm=12, length_mm=40,
                 tolerance=Tolerance(fit="H9")),
        ],
        surface_treatment="painted RAL 7016",
        notes=[
            "Surface treatment: primed and painted RAL 7016, machined faces and holes masked.",
            "Flatness of both faces 0.03.",
            "Casting free of porosity on machined faces.",
            "Remove all burrs.",
        ],
    )  # fmt: skip


# ---------------------------------------------------------------- PS-2045 pinion shaft (spline + teeth)
PINION_SEGMENTS = [  # (x0, x1, radius) left to right
    (0.0, 28.0, 12.5),  # spline W25
    (28.0, 47.0, 15.0),  # bearing seat Ø30 k5
    (47.0, 59.0, 19.0),  # shoulder Ø38
    (59.0, 89.0, 23.75),  # teeth, tip Ø47.5
    (89.0, 111.0, 14.0),  # bearing seat Ø28 j6
    (111.0, 125.0, 10.0),  # thread M20x1.5
]


def draw_pinion(s: Sheet, p: Part) -> None:
    X0, Y0 = 62.0, 176.0

    def X(x: float) -> float:
        return X0 + x

    def Y(y: float) -> float:
        return Y0 + y

    L = 125.0
    s.view_label(X(62), Y(70), "FRONT VIEW")
    s.center([(X(-6), Y0), (X(L + 6), Y0)])
    for x1, x2, r in PINION_SEGMENTS:
        q1 = x1 + (1.5 if x1 == 0 else 0.0)
        q2 = x2 - (1.5 if x2 == L else 0.0)
        s.line([(X(q1), Y(r)), (X(q2), Y(r))])
        s.line([(X(q1), Y(-r)), (X(q2), Y(-r))])
    for (_, x2, r1), (_, _, r2) in zip(PINION_SEGMENTS, PINION_SEGMENTS[1:], strict=False):
        R = max(r1, r2)
        s.line([(X(x2), Y(-R)), (X(x2), Y(R))])
    for xe, r, sgn in ((0.0, 12.5, 1), (L, 10.0, -1)):  # chamfers 1.5x45
        s.line([(X(xe), Y(-r + 1.5)), (X(xe), Y(r - 1.5))])
        s.line([(X(xe), Y(r - 1.5)), (X(xe + 1.5 * sgn), Y(r))])
        s.line([(X(xe), Y(-r + 1.5)), (X(xe + 1.5 * sgn), Y(-r))])
        s.thin([(X(xe + 1.5 * sgn), Y(-r)), (X(xe + 1.5 * sgn), Y(r))])
    # spline minor diameter, thread minor diameter (thin), gear pitch + root line
    for r in (11.25, -11.25):
        s.thin([(X(1.5), Y(r)), (X(26), Y(r))])
    for r in (8.4, -8.4):
        s.thin([(X(113), Y(r)), (X(L - 1.5), Y(r))])
    for r in (21.25, -21.25):
        s.center([(X(57), Y(r)), (X(91), Y(r))])
    for r in (20.6, -20.6):
        s.thin([(X(59), Y(r)), (X(89), Y(r))])
    # centre holes (hidden) both ends
    for xe, sgn in ((0.0, 1), (L, -1)):
        s.hidden([(X(xe), Y(3.35)), (X(xe + 3 * sgn), Y(1.6)), (X(xe + 8 * sgn), Y(1.6)),
                  (X(xe + 9 * sgn), Y(0))])  # fmt: skip
        s.hidden([(X(xe), Y(-3.35)), (X(xe + 3 * sgn), Y(-1.6)), (X(xe + 8 * sgn), Y(-1.6)),
                  (X(xe + 9 * sgn), Y(0))])  # fmt: skip

    # diameters carried above the part (staggered heights)
    s.diam_up(X(37.5), Y(-15), Y(15), Y(33), p.ev("F2"))
    s.diam_up(X(53), Y(-19), Y(19), Y(44), p.ev("F3"))
    s.diam_up(X(74), Y(-23.75), Y(23.75), Y(33), p.ev("F5"))
    s.diam_up(X(100), Y(-14), Y(14), Y(44), p.ev("F6"))
    s.leader((X(14), Y(12.5)), (X(-26), Y(56)), [p.ev("F1")], side="right")
    s.leader((X(118), Y(10)), (X(116), Y(30)), [p.ev("F7")], side="right")
    s.leader((X(0.3), Y(2.5)), (X(8), Y(-28)), [p.ev("F8")], side="right")
    s.leader((X(L - 0.7), Y(-9.3)), (X(L + 2), Y(-26)), [p.ev("F9")], side="right")
    # lengths (chain + overall)
    chain = [0.0, 28.0, 47.0, 59.0, 89.0, 111.0, 125.0]
    rad = {x: r for x, _, r in PINION_SEGMENTS}
    for a, b in zip(chain, chain[1:], strict=False):
        s.hdim(X(a), X(b), Y(-38), f"{b - a:g}", ext_from=(Y(-rad[a]), Y(-rad.get(b, 10.0))))
    s.hdim(X(0), X(L), Y(-52), "125", ext_from=(Y(-39), Y(-39)))


def pinion() -> Part:
    return Part(
        pn="PS-2045",
        rev="D",
        title="Pinion Shaft",
        material="C45E",
        customer="Lumbra Robotics S.r.l.",
        drawn_by="L. Fischbach",
        drawn_on="11.09.2026",
        envelope=Envelope(shape_class=ShapeClass.ROTATIONAL, max_diameter_mm=47.5, length_mm=125),
        draw=draw_pinion,
        general_tolerance="ISO 2768-m",
        default_ra=1.6,
        layout="alt",
        notes_heading="NOTES",
        data_heading="TOOTH DATA",
        features=[
            feat("F1", FeatureType.SPLINE, "DIN 5480-W25×1.25×18×8f", "front view",
                 "Involute spline W25 x 1.25 x 18 (DIN 5480), length 28", nominal_mm=25, length_mm=28),
            feat("F2", FeatureType.OUTER_DIAMETER, "Ø30 k5", "front view", "Bearing seat Ø30 k5",
                 nominal_mm=30, length_mm=19, tolerance=Tolerance(fit="k5")),
            feat("F3", FeatureType.OUTER_DIAMETER, "Ø38", "front view", "Shoulder Ø38",
                 nominal_mm=38, length_mm=12),
            feat("F4", FeatureType.GEAR_TEETH, "m = 2.5", "tooth data",
                 "Integral spur teeth m 2.5, z 17, pressure angle 20°, pitch diameter 42.5, face width 30",
                 nominal_mm=42.5, gear_module=2.5, gear_teeth=17, face_width_mm=30),
            feat("F5", FeatureType.OUTER_DIAMETER, "Ø47.5 h10", "front view", "Tip diameter Ø47.5 h10",
                 nominal_mm=47.5, length_mm=30, tolerance=Tolerance(fit="h10")),
            feat("F6", FeatureType.OUTER_DIAMETER, "Ø28 j6", "front view", "Bearing seat Ø28 j6",
                 nominal_mm=28, length_mm=22, tolerance=Tolerance(fit="j6")),
            feat("F7", FeatureType.THREAD, "M20×1.5-6g", "front view", "Thread M20x1.5-6g, length 14",
                 nominal_mm=20, length_mm=14, thread_spec="M20x1.5"),
            feat("F8", FeatureType.HOLE, "2× DIN 332-A3.15/6.7", "front view",
                 "Centre holes DIN 332 form A 3.15/6.7 at both ends", nominal_mm=3.15, quantity=2),
            feat("F9", FeatureType.CHAMFER, "2× 1.5×45°", "front view", "Chamfer 1.5x45° at both ends",
                 nominal_mm=1.5, quantity=2),
        ],
        heat_treatment="induction hardened 50-56 HRC",
        notes=[
            "Teeth and bearing seats induction hardened 50-56 HRC, depth 1.0-1.5 mm.",
            "Bearing seats Ø30 k5 and Ø28 j6: runout 0.008 to common axis.",
            "Teeth ground after hardening.",
        ],
        data_table=[
            "Module|m = 2.5",
            "Number of teeth|z = 17",
            "Pressure angle|α = 20°",
            "Profile shift|x = 0",
            "Pitch diameter|d = 42.5",
            "Face width|b = 30",
            "Accuracy|ISO 1328-1 grade 8",
        ],
    )  # fmt: skip


def holdout_parts() -> list[Part]:
    return [bushing(), plate(), pinion()]


# ---------------------------------------------------------------- RFQ emails + gold RFQRequest
@dataclass
class Rfq:
    rfq_id: str
    part: Part
    email: str
    gold: RFQRequest


def rfqs() -> list[Rfq]:
    sh, fl, br, asm = shaft(), flange(), bracket(), assembly()
    return [
        Rfq(
            "RFQ-2026-0101",
            sh,
            f"""From: Jonas Brückner <j.brueckner@hallvig-foerdertechnik.example>
To: {OUR_SALES}
Subject: Anfrage Abtriebswelle SH-4711 - 200 / 500 Stück
Date: Tue, 15 Sep 2026 09:42:17 +0200
Attachments: SH-4711.pdf

Sehr geehrte Damen und Herren,

für die Serienfertigung unseres neuen Kettenfördergetriebes bitten wir um Ihr Angebot für
folgende Position:

Pos. 1: Abtriebswelle, Zeichnungs-Nr. SH-4711, Index B (siehe Anhang SH-4711.pdf)
Mengen: 200 Stück sowie 500 Stück (bitte Staffelpreise angeben)
Gewünschter Liefertermin: 27.11.2026

Weitere Anforderungen:
- Werkstoff und Wärmebehandlung gemäß Zeichnung
- Abnahmeprüfzeugnis 3.1 nach EN 10204 für das Material
- Erstmusterprüfbericht (EMPB) mit der ersten Lieferung

Lieferbedingung: DAP Kassel (Incoterms 2020), Preise bitte in EUR.
Wir bitten um Zusendung Ihres Angebots bis zum 02.10.2026.

Mit freundlichen Grüßen

Jonas Brückner
Strategischer Einkauf
Hallvig Fördertechnik GmbH
""",
            RFQRequest(
                rfq_id="RFQ-2026-0101",
                customer_name="Hallvig Fördertechnik GmbH",
                contact_name="Jonas Brückner",
                contact_email="j.brueckner@hallvig-foerdertechnik.example",
                language="de",
                received_at=date(2026, 9, 15),
                incoterm="DAP",
                currency="EUR",
                special_requirements=["EN 10204 3.1 certificate", "First article inspection report"],
                items=[
                    RFQItem(
                        line_no=1,
                        customer_part_number="SH-4711",
                        description="Abtriebswelle",
                        drawing_ref="SH-4711.pdf",
                        quantities=[200, 500],
                        requested_delivery=date(2026, 11, 27),
                        notes="Werkstoff und Wärmebehandlung gemäß Zeichnung",
                    )
                ],
            ),
        ),
        Rfq(
            "RFQ-2026-0102",
            fl,
            f"""From: Emily Hartwell <e.hartwell@norvane-motion.example>
To: {OUR_SALES}
Subject: RFQ - Motor flange FL-2208, 1,000 pcs
Date: Wed, 16 Sep 2026 14:05:51 +0100
Attachments: FL-2208.pdf

Dear Sales Team,

please quote the following item for our servo drive series:

Item 1: Motor Flange, drawing FL-2208 rev. A (attached as FL-2208.pdf)
Quantity: 1,000 pcs
Requested delivery: 4 December 2026

Requirements:
- Black anodizing as per drawing, RoHS compliant
- Parts packed in VCI bags, max. 50 pcs per box

Delivery terms FCA your works, prices in EUR please.
We would appreciate your quotation by 30 September.

Kind regards,
Emily Hartwell
Senior Buyer
Norvane Motion Systems Ltd
""",
            RFQRequest(
                rfq_id="RFQ-2026-0102",
                customer_name="Norvane Motion Systems Ltd",
                contact_name="Emily Hartwell",
                contact_email="e.hartwell@norvane-motion.example",
                language="en",
                received_at=date(2026, 9, 16),
                incoterm="FCA",
                currency="EUR",
                special_requirements=["RoHS compliant anodizing", "Packed in VCI bags, max. 50 pcs per box"],
                items=[
                    RFQItem(
                        line_no=1,
                        customer_part_number="FL-2208",
                        description="Motor Flange",
                        drawing_ref="FL-2208.pdf",
                        quantities=[1000],
                        requested_delivery=date(2026, 12, 4),
                    )
                ],
            ),
        ),
        Rfq(
            "RFQ-2026-0103",
            br,
            f"""From: Sanne Verbeek <s.verbeek@tervalo-automation.example>
To: {OUR_SALES}
Subject: Request for quotation - Mounting bracket BR-0930
Date: Thu, 17 Sep 2026 11:18:03 +0200
Attachments: BR-0930.pdf

Hello,

for a new pick-and-place module we are looking for a supplier for the following part:

Item 1: Mounting Bracket, drawing no. BR-0930 rev. A (see attachment BR-0930.pdf)
Quantity: 300 pcs
Requested delivery date: 20 November 2026

Please include in your offer:
- First article inspection report with the first delivery
- Parts delivered deburred and cleaned

Delivery DAP Eindhoven. Please quote in EUR.

Best regards,
Sanne Verbeek
Purchasing
Tervalo Automation B.V.
""",
            RFQRequest(
                rfq_id="RFQ-2026-0103",
                customer_name="Tervalo Automation B.V.",
                contact_name="Sanne Verbeek",
                contact_email="s.verbeek@tervalo-automation.example",
                language="en",
                received_at=date(2026, 9, 17),
                incoterm="DAP",
                currency="EUR",
                special_requirements=[
                    "First article inspection report",
                    "Parts delivered deburred and cleaned",
                ],
                items=[
                    RFQItem(
                        line_no=1,
                        customer_part_number="BR-0930",
                        description="Mounting Bracket",
                        drawing_ref="BR-0930.pdf",
                        quantities=[300],
                        requested_delivery=date(2026, 11, 20),
                    )
                ],
            ),
        ),
        Rfq(
            "RFQ-2026-0104",
            asm,
            f"""From: Markus Engelhardt <m.engelhardt@veltrum-aktorik.example>
To: {OUR_SALES}
Subject: Preisanfrage Baugruppe ASM-5100 (Aktuator), 50 Stück
Date: Mon, 21 Sep 2026 16:27:40 +0200
Attachments: ASM-5100.pdf

Guten Tag,

bitte unterbreiten Sie uns ein Angebot für die komplette Montage folgender Baugruppe:

Pos. 1: Aktuator-Unterbaugruppe ASM-5100, Index C (Zeichnung ASM-5100.pdf im Anhang)
Menge: 50 Stück
Liefertermin: 11.12.2026

Die Einzelteile HS-5101 und SH-5102 haben Sie bereits für uns gefertigt. Kaufteile
(Lager, Dichtung, Schrauben, Sicherungsring) bitte gemäß Stückliste beistellen.

Anforderungen:
- Lieferung als komplett montierte Baugruppe
- 100 % Funktionsprüfung mit Prüfprotokoll

Lieferbedingung FCA, Angebot bitte in EUR.

Freundliche Grüße
Markus Engelhardt
Einkauf
Veltrum Aktorik GmbH
""",
            RFQRequest(
                rfq_id="RFQ-2026-0104",
                customer_name="Veltrum Aktorik GmbH",
                contact_name="Markus Engelhardt",
                contact_email="m.engelhardt@veltrum-aktorik.example",
                language="de",
                received_at=date(2026, 9, 21),
                incoterm="FCA",
                currency="EUR",
                special_requirements=[
                    "Delivered as fully assembled unit",
                    "100 % functional test with test report",
                ],
                items=[
                    RFQItem(
                        line_no=1,
                        customer_part_number="ASM-5100",
                        description="Aktuator-Unterbaugruppe",
                        drawing_ref="ASM-5100.pdf",
                        quantities=[50],
                        requested_delivery=date(2026, 12, 11),
                        notes="Purchased parts (bearings, seal, screws, retaining ring) per parts list",
                    )
                ],
            ),
        ),
    ]


def main() -> None:
    problems = []
    for r in rfqs():
        d = SAMPLES_DIR / r.rfq_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "email.txt").write_text(r.email, encoding="utf-8")
        (d / "expected").mkdir(exist_ok=True)
        (d / "expected" / "rfq.json").write_text(r.gold.model_dump_json(indent=2) + "\n", encoding="utf-8")
        pdf, *_, overlaps = render(r.part, d)
        problems += [(r.part.pn, o) for o in overlaps]
        print(f"{r.rfq_id}: {pdf.relative_to(SAMPLES_DIR)}")
    pdf, *_, overlaps = render(gear(), SAMPLES_DIR / "eval_only")
    problems += [("GR-3340", o) for o in overlaps]
    print(f"eval_only: {pdf.relative_to(SAMPLES_DIR)}")
    for part in holdout_parts():
        pdf, *_, overlaps = render(part, HOLDOUT_DIR)
        problems += [(part.pn, o) for o in overlaps]
        print(f"holdout: {pdf.relative_to(SAMPLES_DIR)}")
    for pn, (a, b) in problems:
        print(f"WARNING {pn}: overlapping text {a!r} / {b!r}")
    print(json.dumps({"overlaps": len(problems)}))


if __name__ == "__main__":
    main()
