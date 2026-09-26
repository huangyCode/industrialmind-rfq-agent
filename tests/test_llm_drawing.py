"""Drawing extraction plumbing: render, region crops, 3-call merge (fake provider, no network)."""

from __future__ import annotations

import json

import pymupdf
import pytest

from rfq_agent.agents import drawing
from rfq_agent.agents.drawing import crop_regions, extract_drawing, render_pdf
from rfq_agent.data.repositories import MaterialRepo
from rfq_agent.llm.cache import LLMCache
from rfq_agent.llm.client import LLMClient

MM = 72 / 25.4


@pytest.fixture()
def pdf(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=420 * MM, height=297 * MM)
    # title block at x 235–410, y 10–66 mm from the bottom
    page.draw_rect(pymupdf.Rect(235 * MM, (297 - 66) * MM, 410 * MM, (297 - 10) * MM))
    page.insert_text((240 * MM, (297 - 40) * MM), "TB-4711 Material 1.7225", fontsize=10)
    page.insert_text((240 * MM, 40 * MM), "NOTES: QT 28-32 HRC", fontsize=10)
    page.insert_text((40 * MM, 150 * MM), "Ø35 k6", fontsize=10)
    p = tmp_path / "TB-4711.pdf"
    doc.save(p)
    return p


def test_render_pdf_200dpi(pdf, tmp_path):
    pages = render_pdf(pdf, out_dir=tmp_path / "r")
    assert len(pages) == 1 and pages[0].exists()
    pix = pymupdf.Pixmap(pages[0])
    assert abs(pix.width - 3307) <= 2 and abs(pix.height - 2339) <= 2  # A3 at 200 dpi


def test_crop_regions_follow_layout(pdf, tmp_path):
    page = render_pdf(pdf, out_dir=tmp_path / "r")[0]
    crops = crop_regions(page)
    fixed = {"title_block", "right_column", "views", "overview"}
    assert fixed <= set(crops) and all(k.startswith("view_") for k in set(crops) - fixed)  # + content tiles
    sizes = {k: (pymupdf.Pixmap(v).width, pymupdf.Pixmap(v).height) for k, v in crops.items()}
    w, h = sizes["title_block"]
    assert 1700 > w > 1400 and 700 > h > 500  # ~188 × 69 mm at 200 dpi
    assert 1900 > sizes["views"][0] > 1700 and sizes["views"][1] > 2200  # ~226 × 291 mm
    assert max(sizes["overview"]) <= 1600


class RegionFake:
    """Answers by schema title, so each region call gets its own canned response."""

    name = "fake"

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls: list[dict] = []

    def model_for(self, has_images):
        return "fake-vl"

    def available(self, model=None):
        return True

    def complete(self, *, model, system, messages, schema, max_tokens):
        self.calls.append({"title": schema["title"], "images": messages[0]["images"], "system": system})
        return json.dumps(self.answers[schema["title"]]), {"input_tokens": 1, "output_tokens": 1}


def test_extract_drawing_merges_regions(pdf, tmp_path, db, monkeypatch):
    monkeypatch.setattr(drawing, "RENDER_DIR", tmp_path / "r")
    monkeypatch.setattr(drawing.render_pdf, "__defaults__", (200, tmp_path / "r"))
    fake = RegionFake(
        {
            "TitleBlockExtract": {
                "title_block": {
                    "part_number": "TB-4711",
                    "material": "1.7225",
                    "evidence": {"material": {"text": "1.7225"}},
                },
            },
            "NotesExtract": {"notes": ["Quenched and tempered 28-32 HRC"], "heat_treatment": "QT 28-32 HRC"},
            "ViewsExtract": {
                "envelope": {"shape_class": "rotational", "max_diameter_mm": 45, "length_mm": 225},
                "features": [
                    {
                        "id": "F1",
                        "type": "outer_diameter",
                        "description": "bearing seat",
                        "nominal_mm": 35,
                        "tolerance": {"fit": "k6"},
                        "evidence": {"text": "Ø35 k6", "location": "front view"},
                    }
                ],
            },
        }
    )
    client = LLMClient(fake, mode="live", cache=LLMCache(tmp_path / "c"), failure_dir=tmp_path / "f")
    spec, pages = extract_drawing(pdf, client, MaterialRepo(db))
    assert [c["title"] for c in fake.calls] == ["TitleBlockExtract", "NotesExtract", "ViewsExtract"]
    assert len(fake.calls[2]["images"]) == 2  # overview + zoomed views crop
    assert "REGION: title block" in fake.calls[0]["system"] and "NEVER invent" in fake.calls[0]["system"]
    assert spec.drawing_file == "TB-4711.pdf"
    assert spec.title_block.material_code == "42CrMo4"
    assert spec.heat_treatment == "QT 28-32 HRC"
    seat = next(f for f in spec.features if f.nominal_mm == 35)
    assert seat.tolerance.it_grade == 6
    # the largest Ø was only given as the envelope: added as an outer-diameter feature with a warning
    assert any(f.type == "outer_diameter" and f.nominal_mm == 45 for f in spec.features)
    assert pages[0].exists()


def test_title_block_failure_degrades_to_warning(pdf, tmp_path, db, monkeypatch):
    monkeypatch.setattr(drawing.render_pdf, "__defaults__", (200, tmp_path / "r"))
    fake = RegionFake(
        {
            "TitleBlockExtract": {"title_block": "garbage"},
            "NotesExtract": {},
            "ViewsExtract": {"envelope": {"shape_class": "prismatic"}},
        }
    )
    client = LLMClient(fake, mode="live", cache=LLMCache(tmp_path / "c"), failure_dir=tmp_path / "f")
    spec, _ = extract_drawing(pdf, client, MaterialRepo(db))
    assert any(w.startswith("Title block not extracted") for w in spec.extraction_warnings)
    assert len(fake.calls) == 3 * 1 + 2  # title block tried 3x, notes + views once each
