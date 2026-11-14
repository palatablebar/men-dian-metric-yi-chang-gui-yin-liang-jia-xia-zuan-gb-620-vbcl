"""数据修订控制器：补报形成修订 / 列表 / 详情（前后对比）/ 确认。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, revision_service


def list_revisions(db: Session, **filters) -> dict:
    return revision_service.list_revisions(db, **filters)


def get_revision(db: Session, revision_id: int) -> dict:
    return revision_service.serialize_detail(db, revision_service.get_revision(db, revision_id))


def ingest(db: Session, payload: dict, principal: dict, request: Request) -> tuple[dict, bool]:
    revision, created = revision_service.ingest(db, payload, principal["email"])
    if created:
        stats = revision.stats or {}
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="revision.create",
            entity="revision",
            entity_id=revision.id,
            detail={
                "revision_no": revision.revision_no,
                "data_cutoff": revision.data_cutoff.isoformat(timespec="seconds"),
                "source": revision.source,
                "changed_points": stats.get("changed_points", 0),
                "reviews_reset": (stats.get("anomalies") or {}).get("reviews_reset", 0),
                "reconciled": (stats.get("reconciliation") or {}).get("ok"),
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return revision_service.serialize_detail(db, revision), created


def confirm(db: Session, revision_id: int, principal: dict, request: Request) -> dict:
    revision = revision_service.get_revision(db, revision_id)
    was_draft = revision.status != "confirmed"
    revision_service.confirm(db, revision, principal["email"])
    if was_draft:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="revision.confirm",
            entity="revision",
            entity_id=revision.id,
            detail={"revision_no": revision.revision_no, "data_cutoff": revision.data_cutoff.isoformat(timespec="seconds")},
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return revision_service.serialize_detail(db, revision)
