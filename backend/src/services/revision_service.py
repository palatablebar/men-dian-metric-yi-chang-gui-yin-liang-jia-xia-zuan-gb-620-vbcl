"""数据修订服务：迟到补报 ingestion → 按数据截止时间形成修订 → 异常重算 → 复核回退 → 对平。

链路：upsert 指标点（记录前后水位）→ 对受影响门店重跑算法 A → 方向/影响档位变化时归档原复核
并回到待复核 → 重建归因（算法 B）→ 量价三项/渠道贡献/同行对比与同期明细对平 → 写入修订统计。
幂等：同一 `data_cutoff` 重试直接返回已有修订，不重复落数据、不新增修订。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    METRIC_LABELS,
    REVISION_SOURCE_LABELS,
    REVISION_STATUS_LABELS,
    SEVERITY_LABELS,
)
from src.models.anomaly import Anomaly
from src.models.attribution import Attribution
from src.models.metric import MetricPoint
from src.models.report import Report
from src.models.revision import DataRevision, ReviewArchive
from src.models.store import Store
from src.services import attribution_service, detection, metric_service, revision_core
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, parse_date, utcnow

DETECTION_PARAMS = {"window": 28, "threshold": 3.5, "persist_z": 1.5, "stratify": True}


def _iso_cutoff(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def serialize(revision: DataRevision) -> dict:
    stats = revision.stats or {}
    anomalies = stats.get("anomalies") or {}
    reconciliation = stats.get("reconciliation") or {}
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "data_cutoff": _iso_cutoff(revision.data_cutoff),
        "source": revision.source,
        "source_label": REVISION_SOURCE_LABELS.get(revision.source, revision.source),
        "status": revision.status,
        "status_label": REVISION_STATUS_LABELS.get(revision.status, revision.status),
        "note": revision.note,
        "changed_points": stats.get("changed_points", 0),
        "affected_dates": stats.get("affected_dates", []),
        "affected_anomalies": anomalies.get("recomputed", 0),
        "reviews_reset": anomalies.get("reviews_reset", 0),
        "reconciled": reconciliation.get("ok"),
        "max_abs_diff": reconciliation.get("max_abs_diff"),
        "created_by": revision.created_by,
        "confirmed_by": revision.confirmed_by,
        "confirmed_at": iso_utc(revision.confirmed_at),
        "created_at": iso_utc(revision.created_at),
    }


def serialize_detail(db: Session, revision: DataRevision) -> dict:
    data = serialize(revision)
    stats = revision.stats or {}
    data["items"] = stats.get("items", [])
    data["anomaly_changes"] = stats.get("anomaly_changes", [])
    data["anomalies"] = stats.get("anomalies", {})
    data["reconciliation"] = stats.get("reconciliation", {})
    data["affected_reports"] = affected_reports(db, revision)
    return data


def next_revision_no(db: Session) -> str:
    from src.utils.timeutil import local_now

    prefix = f"RV{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(DataRevision).where(DataRevision.revision_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def get_revision(db: Session, revision_id: int) -> DataRevision:
    revision = db.get(DataRevision, revision_id)
    if revision is None:
        raise not_found("revision", revision_id)
    return revision


def latest_confirmed(db: Session) -> DataRevision | None:
    return db.scalar(
        select(DataRevision)
        .where(DataRevision.status == "confirmed")
        .order_by(DataRevision.data_cutoff.desc(), DataRevision.id.desc())
        .limit(1)
    )


def list_revisions(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(DataRevision)
    count_stmt = select(func.count()).select_from(DataRevision)
    if status:
        stmt = stmt.where(DataRevision.status == status)
        count_stmt = count_stmt.where(DataRevision.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(DataRevision.data_cutoff.desc(), DataRevision.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def affected_reports(db: Session, revision: DataRevision) -> list[dict]:
    """统计区间覆盖修订变更日期、但尚未引用该修订的报告（引用旧版数据的受影响报告）。"""
    dates = {parse_date(text) for text in (revision.stats or {}).get("affected_dates", [])}
    dates.discard(None)
    if not dates:
        return []
    revision_nos = {row.id: row.revision_no for row in db.scalars(select(DataRevision)).all()}
    affected = []
    for report in db.scalars(select(Report).order_by(Report.id.desc())).all():
        if report.revision_id == revision.id:
            continue
        if not any(report.period_start <= day <= report.period_end for day in dates):
            continue
        affected.append(
            {
                "id": report.id,
                "report_no": report.report_no,
                "title": report.title,
                "status": report.status,
                "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(),
                "revision_id": report.revision_id if report.revision_id in revision_nos else None,
                "revision_no": revision_nos.get(report.revision_id),
            }
        )
    return affected


def _apply_points(db: Session, points: list[dict]) -> tuple[list[dict], set[date], set[int]]:
    """upsert 补报指标点，记录每点前后水位。口径：渠道行 revenue = adr × rooms_sold。"""
    stores = {store.code: store for store in db.scalars(select(Store)).all()}
    items: list[dict] = []
    dates: set[date] = set()
    store_ids: set[int] = set()
    for point in points:
        store = stores.get(point["store_code"])
        if store is None:
            raise AppError("VALIDATION", 422, {"field": "store_code", "message": f"门店不存在: {point['store_code']}"})
        stat_date = parse_date(point["stat_date"])
        if stat_date is None:
            raise AppError("VALIDATION", 422, {"field": "stat_date", "message": f"非法日期: {point['stat_date']}"})
        existing = db.scalar(
            select(MetricPoint).where(
                MetricPoint.store_id == store.id,
                MetricPoint.stat_date == stat_date,
                MetricPoint.channel == point["channel"],
            )
        )
        rooms_available = point.get("rooms_available") or (existing.rooms_available if existing else store.room_count)
        rooms_sold = int(point["rooms_sold"])
        adr = round(float(point["adr"]), 2)
        after = {
            "rooms_available": rooms_available,
            "rooms_sold": rooms_sold,
            "adr": adr,
            "revenue": round(adr * rooms_sold, 2),
            "occupancy": round(rooms_sold / rooms_available, 6) if rooms_available else 0.0,
        }
        before = None
        if existing is None:
            db.add(
                MetricPoint(
                    store_id=store.id,
                    stat_date=stat_date,
                    channel=point["channel"],
                    rooms_available=after["rooms_available"],
                    rooms_sold=after["rooms_sold"],
                    occupancy=after["occupancy"],
                    adr=after["adr"],
                    revenue=after["revenue"],
                    is_weekend=stat_date.weekday() >= 5,
                    room_type_mix={},
                )
            )
        else:
            before = {
                "rooms_available": existing.rooms_available,
                "rooms_sold": existing.rooms_sold,
                "adr": round(existing.adr, 2),
                "revenue": round(existing.revenue, 2),
                "occupancy": round(existing.occupancy, 6),
            }
            existing.rooms_available = after["rooms_available"]
            existing.rooms_sold = after["rooms_sold"]
            existing.adr = after["adr"]
            existing.revenue = after["revenue"]
            existing.occupancy = after["occupancy"]
        items.append(
            {
                "store_code": store.code,
                "store_name": store.name,
                "stat_date": stat_date.isoformat(),
                "channel": point["channel"],
                "before": before,
                "after": after,
                "delta": revision_core.diff_water_levels(before, after),
            }
        )
        dates.add(stat_date)
        store_ids.add(store.id)
    db.flush()
    return items, dates, store_ids


def _archive_review(db: Session, anomaly: Anomaly, revision: DataRevision, hit: dict) -> ReviewArchive:
    """保留原复核：归档结论与前后水位，异常回到待复核。"""
    archive = ReviewArchive(
        anomaly_id=anomaly.id,
        revision_id=revision.id,
        status=anomaly.status,
        reviewer=anomaly.reviewer,
        review_note=anomaly.review_note,
        reviewed_at=anomaly.reviewed_at,
        prev_direction=anomaly.direction,
        prev_severity=anomaly.severity,
        prev_impact=anomaly.impact_amount,
        prev_z=anomaly.robust_z,
        new_direction=hit["direction"],
        new_severity=hit["severity"],
        new_impact=hit["impact_amount"],
        new_z=hit["robust_z"],
    )
    db.add(archive)
    anomaly.status = "pending"
    anomaly.reviewer = None
    anomaly.review_note = None
    anomaly.reviewed_at = None
    return archive


def _apply_hit(anomaly: Anomaly, hit: dict, revision: DataRevision, run_params: dict | None = None) -> None:
    anomaly.observed = hit["observed"]
    anomaly.baseline = hit["baseline"]
    anomaly.mad = hit["mad"]
    anomaly.robust_z = hit["robust_z"]
    anomaly.direction = hit["direction"]
    anomaly.severity = hit["severity"]
    anomaly.impact_amount = hit["impact_amount"]
    anomaly.revision_id = revision.id
    anomaly.payload = {
        "window_start": hit["window_start"],
        "window_end": hit["window_end"],
        "window_samples": hit["window_samples"],
        "mad_basis": hit["mad_basis"],
        "baseline_triple": hit["baseline_triple"],
        "current_triple": hit["current_triple"],
        "params": run_params or {},
        "revision_no": revision.revision_no,
        "data_cutoff": _iso_cutoff(revision.data_cutoff),
    }


def _rebuild_attribution(db: Session, anomaly: Anomaly, store: Store) -> Attribution:
    existing = db.scalar(select(Attribution).where(Attribution.anomaly_id == anomaly.id))
    if existing is not None:
        db.delete(existing)
        db.flush()
    return attribution_service.build(db, anomaly, store)


def _reconcile_anomaly(db: Session, anomaly: Anomaly, attribution: Attribution, store: Store) -> dict:
    """量价三项 / 渠道贡献 / 同行对比 与同期明细（metric_points 当前值）对平。"""
    detail_current = db.scalar(
        select(func.sum(MetricPoint.revenue)).where(
            MetricPoint.store_id == anomaly.store_id, MetricPoint.stat_date == anomaly.metric_date
        )
    ) or 0.0
    peer_ids = [row.id for row in db.scalars(select(Store).where(Store.region == store.region)).all()]
    detail_peer = db.scalar(
        select(func.sum(MetricPoint.revenue)).where(
            MetricPoint.store_id.in_(peer_ids or [0]), MetricPoint.stat_date == anomaly.metric_date
        )
    ) or 0.0
    dimensions = attribution.dimensions or {}
    channel_rows = (dimensions.get("channel") or {}).get("rows") or []
    peer_rows = (dimensions.get("store") or {}).get("rows") or []
    result = revision_core.reconcile_checks(
        parts={"adr": attribution.adr_part, "occ": attribution.occ_part, "rooms": attribution.rooms_part},
        residual=attribution.residual,
        total_delta=attribution.total_delta,
        cur_rev=attribution.cur_rev,
        channel_current_sum=sum(float(row.get("current") or 0.0) for row in channel_rows),
        peer_current_sum=sum(float(row.get("current") or 0.0) for row in peer_rows),
        detail_current_total=float(detail_current),
        detail_peer_total=float(detail_peer),
    )
    result["anomaly_id"] = anomaly.id
    return result


def _recompute_stores(db: Session, store_ids: set[int], revision: DataRevision) -> tuple[list[dict], dict, list[dict]]:
    """对受影响门店重跑算法 A：更新异常、归档复核、重建归因、逐条对平。"""
    bounds = metric_service.date_bounds(db)
    changes: list[dict] = []
    reconciliations: list[dict] = []
    stats = {"recomputed": 0, "created": 0, "updated": 0, "tier_changed": 0, "direction_changed": 0, "reviews_reset": 0, "stale": 0, "stale_ids": []}
    stores = {store.id: store for store in db.scalars(select(Store).where(Store.id.in_(store_ids))).all()}
    for store_id, store in stores.items():
        series = metric_service.store_series(db, store_id, bounds[0], bounds[1])
        result = detection.detect_series(series, "rev", **DETECTION_PARAMS)
        hits = {hit["date"]: hit for hit in result["hits"]}
        anomalies = db.scalars(
            select(Anomaly).where(Anomaly.store_id == store_id, Anomaly.metric_key == "rev")
        ).all()
        for anomaly in anomalies:
            hit = hits.pop(anomaly.metric_date.isoformat(), None)
            if hit is None:
                stats["stale"] += 1
                stats["stale_ids"].append(anomaly.id)
                continue
            change = revision_core.classify_change(anomaly.direction, anomaly.severity, hit["direction"], hit["severity"])
            archived = None
            if change["reset_required"] and anomaly.status != "pending":
                archive = _archive_review(db, anomaly, revision, hit)
                archived = {
                    "status": archive.status,
                    "reviewer": archive.reviewer,
                    "note": archive.review_note,
                    "reviewed_at": iso_utc(archive.reviewed_at),
                }
                stats["reviews_reset"] += 1
            before = {
                "direction": anomaly.direction,
                "severity": anomaly.severity,
                "impact_amount": round(anomaly.impact_amount, 2),
                "robust_z": round(anomaly.robust_z, 4),
            }
            _apply_hit(anomaly, hit, revision, result.get("params"))
            db.flush()
            attribution = _rebuild_attribution(db, anomaly, store)
            reconciliations.append(_reconcile_anomaly(db, anomaly, attribution, store))
            stats["updated"] += 1
            stats["tier_changed"] += 1 if change["tier_changed"] else 0
            stats["direction_changed"] += 1 if change["direction_changed"] else 0
            changes.append(
                {
                    "anomaly_id": anomaly.id,
                    "store_code": store.code,
                    "store_name": store.name,
                    "metric_date": anomaly.metric_date.isoformat(),
                    "metric_key": anomaly.metric_key,
                    "metric_label": METRIC_LABELS.get(anomaly.metric_key, anomaly.metric_key),
                    "is_new": False,
                    "direction": {"before": before["direction"], "after": hit["direction"]},
                    "severity": {
                        "before": before["severity"],
                        "after": hit["severity"],
                        "before_label": SEVERITY_LABELS.get(before["severity"], before["severity"]),
                        "after_label": SEVERITY_LABELS.get(hit["severity"], hit["severity"]),
                    },
                    "impact_amount": {"before": before["impact_amount"], "after": hit["impact_amount"]},
                    "robust_z": {"before": before["robust_z"], "after": hit["robust_z"]},
                    "review_reset": archived is not None,
                    "archived_review": archived,
                }
            )
        for day, hit in hits.items():
            anomaly = Anomaly(
                run_id=None,
                revision_id=revision.id,
                store_id=store_id,
                metric_date=date.fromisoformat(day),
                metric_key="rev",
                observed=hit["observed"],
                baseline=hit["baseline"],
                mad=hit["mad"],
                robust_z=hit["robust_z"],
                direction=hit["direction"],
                severity=hit["severity"],
                impact_amount=hit["impact_amount"],
                status="pending",
                payload={
                    "window_start": hit["window_start"],
                    "window_end": hit["window_end"],
                    "window_samples": hit["window_samples"],
                    "mad_basis": hit["mad_basis"],
                    "baseline_triple": hit["baseline_triple"],
                    "current_triple": hit["current_triple"],
                    "params": result.get("params") or {},
                    "revision_no": revision.revision_no,
                    "data_cutoff": _iso_cutoff(revision.data_cutoff),
                },
            )
            db.add(anomaly)
            db.flush()
            attribution = _rebuild_attribution(db, anomaly, store)
            reconciliations.append(_reconcile_anomaly(db, anomaly, attribution, store))
            stats["created"] += 1
            changes.append(
                {
                    "anomaly_id": anomaly.id,
                    "store_code": store.code,
                    "store_name": store.name,
                    "metric_date": day,
                    "metric_key": "rev",
                    "metric_label": METRIC_LABELS["rev"],
                    "is_new": True,
                    "direction": {"before": None, "after": hit["direction"]},
                    "severity": {"before": None, "after": hit["severity"], "before_label": None, "after_label": SEVERITY_LABELS.get(hit["severity"], hit["severity"])},
                    "impact_amount": {"before": None, "after": hit["impact_amount"]},
                    "robust_z": {"before": None, "after": hit["robust_z"]},
                    "review_reset": False,
                    "archived_review": None,
                }
            )
    stats["recomputed"] = stats["updated"] + stats["created"]
    return changes, stats, reconciliations


def ingest(db: Session, payload: dict, operator: str, seed_key: str | None = None) -> tuple[DataRevision, bool]:
    """迟到补报落库并按数据截止时间形成修订；同一截止时间重试幂等返回已有修订。"""
    try:
        cutoff = revision_core.normalize_cutoff(payload["data_cutoff"])
    except (KeyError, ValueError) as exc:
        raise AppError("VALIDATION", 422, {"field": "data_cutoff", "message": str(exc)}) from exc
    if seed_key:
        existing = db.scalar(select(DataRevision).where(DataRevision.seed_key == seed_key))
        if existing is not None:
            return existing, False
    existing = db.scalar(select(DataRevision).where(DataRevision.data_cutoff == cutoff))
    if existing is not None:
        return existing, False

    revision = DataRevision(
        revision_no=next_revision_no(db),
        data_cutoff=cutoff,
        source=payload.get("source") or "mixed",
        status="draft",
        note=payload.get("note"),
        created_by=operator,
        seed_key=seed_key,
    )
    db.add(revision)
    db.flush()

    items, dates, store_ids = _apply_points(db, payload.get("points") or [])
    changes, anomaly_stats, reconciliations = _recompute_stores(db, store_ids, revision)
    max_abs_diff = max([entry["max_abs_diff"] for entry in reconciliations], default=0.0)
    revision.stats = {
        "changed_points": len(items),
        "affected_dates": sorted(day.isoformat() for day in dates),
        "affected_stores": len(store_ids),
        "items": items,
        "anomalies": anomaly_stats,
        "anomaly_changes": changes,
        "reconciliation": {
            "ok": all(entry["ok"] for entry in reconciliations),
            "checked": len(reconciliations),
            "max_abs_diff": max_abs_diff,
            "tolerance": revision_core.RECONCILE_TOLERANCE,
            "per_anomaly": reconciliations,
        },
    }
    db.flush()
    return revision, True


def confirm(db: Session, revision: DataRevision, operator: str) -> DataRevision:
    """确认修订（幂等）：已确认的修订才能被报告引用。"""
    if revision.status == "confirmed":
        return revision
    revision.status = "confirmed"
    revision.confirmed_by = operator
    revision.confirmed_at = utcnow()
    db.flush()
    return revision
