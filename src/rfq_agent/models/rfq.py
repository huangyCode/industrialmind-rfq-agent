from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class RFQItem(BaseModel):
    line_no: int = Field(description="1-based line number in the order the items appear in the email")
    customer_part_number: str | None = Field(None, description="Part number as written by the customer")
    description: str | None = None
    drawing_ref: str = Field(description="File name of the attached drawing for this item")
    quantities: list[int] = Field(
        default_factory=list, description="All requested quantities (quantity breaks)"
    )
    requested_delivery: date | None = Field(None, description="Requested delivery date, ISO format")
    notes: str | None = None


class RFQRequest(BaseModel):
    rfq_id: str
    customer_name: str
    contact_name: str | None = None
    contact_email: str | None = None
    language: Literal["de", "en", "pl", "zh"] = Field(description="Language of the email body")
    received_at: date | None = None
    incoterm: str | None = Field(None, description="e.g. DAP, FCA, EXW")
    currency: str = "EUR"
    special_requirements: list[str] = Field(
        default_factory=list, description="e.g. 'EN 10204 3.1 certificate', 'First article inspection'"
    )
    items: list[RFQItem]
