"""权限矩阵与请求参数校验（对应 403 / 422 口径）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.constants.enums import Role
from src.constants.roles import EXPORT_ROLES, PERMISSIONS, REVISION_CONFIRM_ROLES, ROLE_LABELS, has_permission
from src.schemas.anomaly import ReviewIn
from src.schemas.revision import RevisionIn
from src.schemas.run import RunIn
from src.schemas.store import StoreIn
from src.utils.pagination import page_params


def test_viewer_has_no_write_or_export_or_audit_permission():
    role = Role.VIEWER.value
    for permission in ("store:write", "export:read", "audit:read", "report:generate", "anomaly:review"):
        assert has_permission(role, permission) is False
    for permission in ("overview:read", "store:read", "anomaly:read", "report:read"):
        assert has_permission(role, permission) is True


def test_export_roles_exclude_viewer():
    assert Role.VIEWER.value not in EXPORT_ROLES
    assert Role.ADMIN.value in EXPORT_ROLES and Role.ANALYST.value in EXPORT_ROLES


def test_analyst_cannot_generate_report_or_write_store():
    assert has_permission("analyst", "report:generate") is False
    assert has_permission("analyst", "store:write") is False
    assert has_permission("analyst", "run:create") is True
    assert set(PERMISSIONS["viewer"]).issubset(PERMISSIONS["analyst"] | {"metric:read"})


def test_revision_role_matrix():
    assert has_permission("viewer", "revision:read") is True
    assert has_permission("viewer", "revision:create") is False
    assert has_permission("analyst", "revision:create") is True
    assert has_permission("analyst", "revision:confirm") is False
    assert set(REVISION_CONFIRM_ROLES) == {Role.ADMIN.value}


def test_revision_payload_validation():
    with pytest.raises(ValidationError):
        RevisionIn(data_cutoff="昨天", points=[])
    with pytest.raises(ValidationError):
        RevisionIn(
            data_cutoff="2026-11-04T06:30",
            points=[{"store_code": "bad", "stat_date": "2026-09-24", "channel": "ota", "rooms_sold": 68, "adr": 1138.59}],
        )
    payload = RevisionIn(
        data_cutoff="2026-11-04T06:30",
        source="ota",
        points=[{"store_code": "LB-HN-001", "stat_date": "2026-09-24", "channel": "ota", "rooms_sold": 68, "adr": 1138.59}],
    )
    assert payload.source == "ota" and payload.points[0].rooms_sold == 68


def test_role_labels_cover_three_roles():
    assert set(ROLE_LABELS) == {"admin", "analyst", "viewer"}


def test_review_payload_validation():
    with pytest.raises(ValidationError):
        ReviewIn(status="approved")
    assert ReviewIn(status="confirmed", note="ok").status == "confirmed"


def test_store_payload_validation():
    with pytest.raises(ValidationError):
        StoreIn(code="非法 编码", name="x", region="华东", city="上海")
    with pytest.raises(ValidationError):
        StoreIn(code="LB-HD-999", name="x", region="华东", city="上海", status="unknown")
    payload = StoreIn(code="LB-HD-999", name="蓝湾·测试店", region="华东", city="上海")
    assert payload.room_count == 200


def test_run_payload_ranges():
    with pytest.raises(ValidationError):
        RunIn(window=3)
    with pytest.raises(ValidationError):
        RunIn(trigger="cron")
    payload = RunIn()
    assert payload.metrics == ["rev"] and payload.threshold == 3.5


def test_pagination_clamped():
    assert page_params(0, -5).limit == 1
    assert page_params(9999, -5).offset == 0
    assert page_params(500, 0).limit == 200
