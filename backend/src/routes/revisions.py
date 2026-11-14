from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES, REVISION_CONFIRM_ROLES, REVISION_CREATE_ROLES
from src.controllers import revision_controller
from src.middlewares.rbac import require_roles
from src.schemas.revision import RevisionIn

router = APIRouter(prefix="/revisions", tags=["revisions"])


@router.get("")
def list_revisions(
    status: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return revision_controller.list_revisions(db, status=status, limit=limit, offset=offset)


@router.post("")
def ingest_revision(
    payload: RevisionIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REVISION_CREATE_ROLES)),
) -> dict:
    """迟到补报形成修订；同一数据截止时间重试返回已有修订（200），否则新建（201）。"""
    data, created = revision_controller.ingest(db, payload.model_dump(), principal, request)
    response.status_code = 201 if created else 200
    return {**data, "created": created}


@router.get("/{revision_id}")
def get_revision(
    revision_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return revision_controller.get_revision(db, revision_id)


@router.post("/{revision_id}/confirm")
def confirm_revision(
    revision_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*REVISION_CONFIRM_ROLES)),
) -> dict:
    return revision_controller.confirm(db, revision_id, principal, request)
