"""报告控制器：生成 / 发布 / 详情。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.models.revision import DataRevision
from src.services import audit_service, report_service


def list_reports(db: Session, **filters) -> dict:
    return report_service.list_reports(db, **filters)


def get_report(db: Session, report_id: int) -> dict:
    report = report_service.get_report(db, report_id)
    revision = db.get(DataRevision, report.revision_id) if report.revision_id else None
    return report_service.serialize(report, revision)


def generate(db: Session, payload: dict, principal: dict, request: Request) -> tuple[dict, bool]:
    report, created = report_service.generate(db, payload, principal["email"])
    if created:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="report.generate",
            entity="report",
            entity_id=report.id,
            detail={
                "report_no": report.report_no,
                "anomalies": report.anomaly_count,
                "impact": report.impact_total,
                "revision_no": ((report.snapshot or {}).get("revision") or {}).get("revision_no"),
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    revision = db.get(DataRevision, report.revision_id) if report.revision_id else None
    return report_service.serialize(report, revision), created


def publish(db: Session, report_id: int, payload: dict, principal: dict, request: Request) -> dict:
    report = report_service.get_report(db, report_id)
    report_service.publish(db, report, payload["status"], payload.get("note"))
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="report.publish",
        entity="report",
        entity_id=report.id,
        detail={"status": report.status, "revision_id": report.revision_id},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    revision = db.get(DataRevision, report.revision_id) if report.revision_id else None
    return report_service.serialize(report, revision)
