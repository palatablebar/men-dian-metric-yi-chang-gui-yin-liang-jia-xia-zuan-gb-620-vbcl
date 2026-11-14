"""数据修订纯逻辑与 seed 修订不变量（对应「迟到补报 → 修订 → 回退/对平」口径）。"""
from __future__ import annotations

import pytest

from scripts.demo_config import SEED_REVISION
from scripts.demo_data import build_dataset
from src.services import detection, revision_core

CACHE: dict = {}


def dataset() -> dict:
    if "data" not in CACHE:
        CACHE["data"] = build_dataset()
    return CACHE["data"]


def aggregate_series(points: list[dict], store_code: str) -> list[dict]:
    days: dict = {}
    for point in points:
        if point["store_code"] != store_code:
            continue
        entry = days.setdefault(
            point["stat_date"],
            {"date": point["stat_date"].isoformat(), "rooms_available": point["rooms_available"], "rooms_sold": 0, "revenue": 0.0, "is_weekend": point["is_weekend"]},
        )
        entry["rooms_sold"] += point["rooms_sold"]
        entry["revenue"] += point["revenue"]
    series = []
    for stat_date, entry in sorted(days.items()):
        revenue = round(entry["revenue"], 2)
        series.append(
            {
                "date": entry["date"],
                "rooms_available": entry["rooms_available"],
                "rooms_sold": entry["rooms_sold"],
                "occupancy": round(entry["rooms_sold"] / entry["rooms_available"], 6),
                "adr": round(revenue / entry["rooms_sold"], 6) if entry["rooms_sold"] else 0.0,
                "revenue": revenue,
                "is_weekend": entry["is_weekend"],
            }
        )
    return series


def apply_seed_revision(points: list[dict]) -> list[dict]:
    patched = []
    for point in points:
        point = dict(point)
        for late in SEED_REVISION["points"]:
            if (
                point["store_code"] == late["store_code"]
                and point["stat_date"].isoformat() == late["stat_date"]
                and point["channel"] == late["channel"]
            ):
                point["rooms_sold"] = late["rooms_sold"]
                point["adr"] = late["adr"]
                point["revenue"] = round(late["adr"] * late["rooms_sold"], 2)
                point["occupancy"] = round(late["rooms_sold"] / point["rooms_available"], 6)
        patched.append(point)
    return patched


def test_classify_change_direction_and_tier():
    assert revision_core.classify_change("down", "high", "down", "medium") == {
        "direction_changed": False,
        "tier_changed": True,
        "reset_required": True,
    }
    assert revision_core.classify_change("down", "medium", "up", "medium")["reset_required"] is True
    assert revision_core.classify_change("down", "medium", "down", "medium")["reset_required"] is False


def test_diff_water_levels_insert_and_update():
    after = {"revenue": 100.0, "adr": 50.0, "rooms_sold": 2, "occupancy": 0.5}
    assert revision_core.diff_water_levels(None, after)["revenue"] == 100.0
    before = {"revenue": 80.0, "adr": 40.0, "rooms_sold": 2, "occupancy": 0.5}
    delta = revision_core.diff_water_levels(before, after)
    assert delta["revenue"] == 20.0 and delta["rooms_sold"] == 0


def test_reconcile_checks_ok_and_failures():
    ok = revision_core.reconcile_checks(
        parts={"adr": -60.0, "occ": -30.0, "rooms": -8.0},
        residual=-2.0,
        total_delta=-100.0,
        cur_rev=900.0,
        channel_current_sum=900.0,
        peer_current_sum=5400.0,
        detail_current_total=900.0,
        detail_peer_total=5400.0,
    )
    assert ok["ok"] is True and ok["max_abs_diff"] == 0.0
    bad_channel = revision_core.reconcile_checks(
        parts={"adr": -60.0, "occ": -30.0, "rooms": -8.0},
        residual=-2.0,
        total_delta=-100.0,
        cur_rev=900.0,
        channel_current_sum=850.0,
        peer_current_sum=5400.0,
        detail_current_total=900.0,
        detail_peer_total=5400.0,
    )
    assert bad_channel["ok"] is False
    assert bad_channel["checks"]["channel"]["ok"] is False
    assert bad_channel["checks"]["volume_price"]["ok"] is True


def test_normalize_cutoff_and_dedupe_key():
    assert revision_core.normalize_cutoff("2026-11-04").isoformat() == "2026-11-04T23:59:59"
    assert revision_core.normalize_cutoff("2026-11-04 06:30").isoformat() == "2026-11-04T06:30:00"
    assert revision_core.normalize_cutoff("2026-11-04T06:30:00").isoformat() == "2026-11-04T06:30:00"
    with pytest.raises(ValueError):
        revision_core.normalize_cutoff("2026-13-40T99:99:99")
    assert revision_core.report_dedupe_key("weekly", "2026-10-26", "2026-11-01", None) == "weekly|2026-10-26|2026-11-01|rev:0"
    assert revision_core.report_dedupe_key("weekly", "2026-10-26", "2026-11-01", 7).endswith("rev:7")


def test_seed_revision_shifts_tier_and_keeps_hit():
    """seed 修订：LB-HN-001 档位 high→medium；LB-XN-005 档位不变；两店仍各只有 1 起命中。"""
    data = dataset()
    patched = apply_seed_revision(data["points"])
    hn_hits = detection.detect_series(aggregate_series(patched, "LB-HN-001"), "rev")["hits"]
    assert len(hn_hits) == 1
    assert hn_hits[0]["date"] == "2026-09-24"
    assert hn_hits[0]["severity"] == "medium"
    assert hn_hits[0]["direction"] == "down"
    xn_hits = detection.detect_series(aggregate_series(patched, "LB-XN-005"), "rev")["hits"]
    assert len(xn_hits) == 1
    assert xn_hits[0]["date"] == "2026-10-26"
    assert xn_hits[0]["severity"] == "medium"


def test_seed_revision_points_reference_dataset():
    data = dataset()
    keys = {(point["store_code"], point["stat_date"].isoformat(), point["channel"]) for point in data["points"]}
    for late in SEED_REVISION["points"]:
        assert (late["store_code"], late["stat_date"], late["channel"]) in keys
