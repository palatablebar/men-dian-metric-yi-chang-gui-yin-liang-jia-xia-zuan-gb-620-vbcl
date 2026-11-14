"""数据修订（迟到补报按数据截止时间形成的版本）+ 复核历史（修订回退时保留原复核）。

- `DataRevision`：一次 PMS/OTA 迟到补报形成的数据版本，自然键 = `data_cutoff`（数据截止时间），
  重试同一截止时间幂等返回已有修订，不新增。`stats` JSON 存水位变化、受影响异常与对平结果。
- `ReviewArchive`：修订重算导致方向 / 影响档位变化时，原复核结论归档保留，异常回到待复核。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class DataRevision(Base):
    __tablename__ = "data_revisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    revision_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    data_cutoff: Mapped[datetime] = mapped_column(DateTime, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(12), default="mixed")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(160), default="")
    confirmed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReviewArchive(Base):
    __tablename__ = "review_archives"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    anomaly_id: Mapped[int] = mapped_column(ForeignKey("anomalies.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("data_revisions.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prev_direction: Mapped[str] = mapped_column(String(8), default="down")
    prev_severity: Mapped[str] = mapped_column(String(12), default="medium")
    prev_impact: Mapped[float] = mapped_column(Float, default=0.0)
    prev_z: Mapped[float] = mapped_column(Float, default=0.0)
    new_direction: Mapped[str] = mapped_column(String(8), default="down")
    new_severity: Mapped[str] = mapped_column(String(12), default="medium")
    new_impact: Mapped[float] = mapped_column(Float, default=0.0)
    new_z: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
