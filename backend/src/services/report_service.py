"""报告服务：日报/周报生成、发布、快照与导出；报告引用已确认数据修订。

- 生成：绑定最新已确认修订，`dedupe_key`（类型+区间+修订）幂等，重试同一截止时间不新增报告；
- 发布：区间被更新修订覆盖时改绑并刷新快照；失败（如修订未对平）恢复旧版引用，不丢结论。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import METRIC_LABELS, REPORT_KIND_LABELS, REPORT_STATUS_LABELS, SEVERITY_LABELS
from src.models.anomaly import Anomaly
from src.models.metric import MetricPoint
from src.models.report import Report
from src.models.revision import DataRevision
from src.models.store import Store
from src.services import revision_core, revision_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc, local_now, parse_date


def serialize(report: Report, revision: DataRevision | None = None) -> dict:
    snapshot_revision = (report.snapshot or {}).get("revision") or {}
    return {
        "id": report.id,
        "report_no": report.report_no,
        "kind": report.kind,
        "kind_label": REPORT_KIND_LABELS.get(report.kind, report.kind),
        "period_start": iso_date(report.period_start),
        "period_end": iso_date(report.period_end),
        "title": report.title,
        "status": report.status,
        "status_label": REPORT_STATUS_LABELS.get(report.status, report.status),
        "generated_by": report.generated_by,
        "anomaly_count": report.anomaly_count,
        "impact_total": round(report.impact_total, 2),
        "note": report.note,
        "snapshot": report.snapshot or {},
        "revision_id": report.revision_id,
        "revision_no": revision.revision_no if revision else snapshot_revision.get("revision_no"),
        "data_cutoff": snapshot_revision.get("data_cutoff"),
        "created_at": iso_utc(report.created_at),
    }


def next_report_no(db: Session) -> str:
    prefix = f"RP{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(Report).where(Report.report_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def _revision_ref(revision: DataRevision | None) -> dict | None:
    if revision is None:
        return None
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "data_cutoff": revision.data_cutoff.isoformat(timespec="seconds") if revision.data_cutoff else None,
        "source": revision.source,
    }


def build_snapshot(db: Session, start: date, end: date, revision: DataRevision | None = None) -> dict:
    rows = db.scalars(select(Anomaly).where(Anomaly.metric_date >= start, Anomaly.metric_date <= end)).all()
    stores = {store.id: store for store in db.scalars(select(Store)).all()}
    severity = {"high": 0, "medium": 0, "low": 0}
    status = {"pending": 0, "confirmed": 0, "dismissed": 0}
    by_metric: dict[str, int] = {}
    drag: dict[str, dict] = {}
    for row in rows:
        severity[row.severity] = severity.get(row.severity, 0) + 1
        status[row.status] = status.get(row.status, 0) + 1
        by_metric[row.metric_key] = by_metric.get(row.metric_key, 0) + 1
        store = stores.get(row.store_id)
        key = store.name if store else str(row.store_id)
        entry = drag.setdefault(key, {"store": key, "code": store.code if store else "", "impact": 0.0, "count": 0})
        entry["impact"] += row.impact_amount
        entry["count"] += 1
    drag_rows = sorted(drag.values(), key=lambda item: item["impact"])[:5]
    bounds = db.execute(select(func.min(MetricPoint.stat_date), func.max(MetricPoint.stat_date))).one()
    return {
        "period": {"start": iso_date(start), "end": iso_date(end)},
        "data_window": {"start": iso_date(bounds[0]), "end": iso_date(bounds[1])},
        "revision": _revision_ref(revision),
        "anomaly_count": len(rows),
        "impact_total": round(sum(row.impact_amount for row in rows), 2),
        "severity": [
            {"key": key, "label": SEVERITY_LABELS[key], "count": severity.get(key, 0)} for key in ("high", "medium", "low")
        ],
        "review": [{"key": key, "count": value} for key, value in status.items()],
        "by_metric": [{"key": key, "label": METRIC_LABELS.get(key, key), "count": value} for key, value in sorted(by_metric.items())],
        "top_drag_stores": [
            {"store": item["store"], "code": item["code"], "impact": round(item["impact"], 2), "count": item["count"]}
            for item in drag_rows
        ],
        "method": "rolling median+MAD (window=28, |z|>=3.5, 同向 2 天)；LMDI 量价分解",
    }


def generate(db: Session, payload: dict, operator: str) -> tuple[Report, bool]:
    """生成报告并绑定最新已确认修订；同一区间+同一修订重试返回已有报告（不新增）。"""
    kind = payload.get("kind") or "weekly"
    end = parse_date(payload.get("period_end")) or local_now().date()
    if payload.get("period_start"):
        start = parse_date(payload.get("period_start")) or end - timedelta(days=6)
    else:
        start = end - timedelta(days=6 if kind == "weekly" else 0)
    if start > end:
        raise AppError("VALIDATION", 422, {"field": "period_start", "message": "开始日期不能晚于结束日期"})
    revision = revision_service.latest_confirmed(db)
    dedupe_key = revision_core.report_dedupe_key(kind, start.isoformat(), end.isoformat(), revision.id if revision else None)
    existing = db.scalar(select(Report).where(Report.dedupe_key == dedupe_key))
    if existing is not None:
        return existing, False
    snapshot = build_snapshot(db, start, end, revision=revision)
    report = Report(
        report_no=next_report_no(db),
        kind=kind,
        period_start=start,
        period_end=end,
        title=f"蓝湾酒店集团 {start.isoformat()} ~ {end.isoformat()} 经营异常{'周报' if kind == 'weekly' else '日报'}",
        status="draft",
        generated_by=operator,
        anomaly_count=snapshot["anomaly_count"],
        impact_total=snapshot["impact_total"],
        note=payload.get("note"),
        snapshot=snapshot,
        revision_id=revision.id if revision else None,
        dedupe_key=dedupe_key,
    )
    db.add(report)
    db.flush()
    return report, True


def list_reports(db: Session, *, kind: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(Report)
    count_stmt = select(func.count()).select_from(Report)
    if kind:
        stmt = stmt.where(Report.kind == kind)
        count_stmt = count_stmt.where(Report.kind == kind)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Report.id.desc()).limit(limit).offset(offset)).all()
    revisions = {row.id: row for row in db.scalars(select(DataRevision)).all()}
    return {
        "items": [serialize(row, revisions.get(row.revision_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_report(db: Session, report_id: int) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise not_found("report", report_id)
    return report


def _covers(report: Report, revision: DataRevision) -> bool:
    """修订的变更日期是否落在报告统计区间内。"""
    dates = (revision.stats or {}).get("affected_dates", [])
    return any(report.period_start.isoformat() <= day <= report.period_end.isoformat() for day in dates)


def publish(db: Session, report: Report, status: str, note: str | None) -> Report:
    """发布/撤回。发布时若区间被更新的已确认修订覆盖，则改绑该修订并刷新快照；
    任一步失败（如修订未对平）恢复旧版引用与快照，复核结论不受影响。"""
    previous = {
        "revision_id": report.revision_id,
        "snapshot": report.snapshot,
        "anomaly_count": report.anomaly_count,
        "impact_total": report.impact_total,
        "status": report.status,
        "note": report.note,
    }
    try:
        if status == "published":
            revision = revision_service.latest_confirmed(db)
            if revision is not None and revision.id != report.revision_id and _covers(report, revision):
                reconciliation = (revision.stats or {}).get("reconciliation") or {}
                if not reconciliation.get("ok", False):
                    raise AppError(
                        "CONFLICT",
                        409,
                        {"revision_no": revision.revision_no, "max_abs_diff": reconciliation.get("max_abs_diff")},
                        message=f"数据修订 {revision.revision_no} 与同期明细未对平，不能发布引用该修订的报告",
                    )
                report.revision_id = revision.id
                report.snapshot = build_snapshot(db, report.period_start, report.period_end, revision=revision)
                report.anomaly_count = report.snapshot["anomaly_count"]
                report.impact_total = report.snapshot["impact_total"]
        report.status = status
        if note is not None:
            report.note = note
        db.flush()
    except Exception:
        report.revision_id = previous["revision_id"]
        report.snapshot = previous["snapshot"]
        report.anomaly_count = previous["anomaly_count"]
        report.impact_total = previous["impact_total"]
        report.status = previous["status"]
        report.note = previous["note"]
        raise
    return report
