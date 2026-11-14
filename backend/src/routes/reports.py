from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES, REPORT_GENERATE_ROLES
from src.controllers import report_controller
from src.middlewares.rbac import require_roles
from src.schemas.report import ReportIn, ReportPatch

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("")
def list_reports(
    kind: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return report_controller.list_reports(db, kind=kind, limit=limit, offset=offset)


@router.get("/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))) -> dict:
    return report_controller.get_report(db, report_id)


@router.post("")
def generate_report(
    payload: ReportIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REPORT_GENERATE_ROLES)),
) -> dict:
    """生成报告（绑定最新已确认修订）；同一区间+同一修订重试返回已有报告（200），否则新建（201）。"""
    data, created = report_controller.generate(db, payload.model_dump(), principal, request)
    response.status_code = 201 if created else 200
    return {**data, "created": created}


@router.patch("/{report_id}")
def publish_report(
    report_id: int,
    payload: ReportPatch,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REPORT_GENERATE_ROLES)),
) -> dict:
    return report_controller.publish(db, report_id, payload.model_dump(), principal, request)
