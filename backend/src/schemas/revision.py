from __future__ import annotations

from pydantic import BaseModel, Field


class RevisionPointIn(BaseModel):
    store_code: str = Field(pattern=r"^LB-[A-Z]{2}-\d{3}$")
    stat_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    channel: str = Field(pattern=r"^(ota|direct|corp)$")
    rooms_available: int | None = Field(default=None, ge=1, le=5000)
    rooms_sold: int = Field(ge=0, le=5000)
    adr: float = Field(gt=0, le=100000)


class RevisionIn(BaseModel):
    data_cutoff: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
    source: str = Field(default="mixed", pattern=r"^(pms|ota|mixed)$")
    note: str | None = Field(default=None, max_length=500)
    points: list[RevisionPointIn] = Field(min_length=1, max_length=50)
