from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .enums import FeatureType, ShapeClass


class Evidence(BaseModel):
    text: str = Field(description="Verbatim text as printed on the drawing")
    location: str | None = Field(None, description="e.g. 'title block', 'front view', 'notes', 'parts list'")


class Tolerance(BaseModel):
    upper: float | None = Field(None, description="Upper deviation in mm, e.g. +0.018")
    lower: float | None = Field(None, description="Lower deviation in mm, e.g. +0.002")
    fit: str | None = Field(None, description="ISO fit code exactly as printed, e.g. 'k6', 'H7'")
    it_grade: int | None = Field(None, description="Computed in post-processing; leave null")


class Feature(BaseModel):
    id: str = Field(description="F1, F2, ... in reading order")
    type: FeatureType
    description: str
    nominal_mm: float | None = Field(None, description="Diameter / width / thread nominal size in mm")
    length_mm: float | None = Field(None, description="Section length / hole depth / keyway length in mm")
    quantity: int = 1
    tolerance: Tolerance | None = None
    ra_um: float | None = Field(None, description="Surface roughness Ra in micrometres if specified")
    thread_spec: str | None = Field(None, description="e.g. 'M12x1.75'")
    gear_module: float | None = None
    gear_teeth: int | None = None
    face_width_mm: float | None = None
    evidence: Evidence | None = None


class TitleBlock(BaseModel):
    part_number: str | None = None
    revision: str | None = None
    title: str | None = None
    material: str | None = Field(None, description="Material exactly as printed; null if the field is empty")
    material_code: str | None = Field(
        None, description="Mapped to master data in post-processing; leave null"
    )
    general_tolerance: str | None = Field(None, description="e.g. 'ISO 2768-mK'")
    default_ra_um: float | None = None
    scale: str | None = None
    units: Literal["mm", "inch"] = "mm"
    drawn_by: str | None = None
    date: str | None = None
    evidence: dict[str, Evidence] = Field(
        default_factory=dict, description="Field name -> verbatim evidence, for every field you filled"
    )


class Envelope(BaseModel):
    shape_class: ShapeClass
    max_diameter_mm: float | None = None
    length_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None


class PartsListItem(BaseModel):
    item_no: int
    part_number: str | None = None
    description: str
    quantity: int
    material: str | None = None
    standard: str | None = Field(None, description="e.g. 'DIN 625', 'ISO 4762'")


class DrawingSpec(BaseModel):
    drawing_file: str
    title_block: TitleBlock
    envelope: Envelope
    features: list[Feature] = Field(default_factory=list)
    heat_treatment: str | None = Field(None, description="e.g. 'QT 28-32 HRC', 'case hardened 58-62 HRC'")
    surface_treatment: str | None = Field(None, description="e.g. 'anodized black', 'zinc plated'")
    notes: list[str] = Field(default_factory=list)
    parts_list: list[PartsListItem] = Field(default_factory=list)
    extraction_warnings: list[str] = Field(default_factory=list)
