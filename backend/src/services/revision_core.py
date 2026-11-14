"""数据修订纯逻辑：变化判定 / 水位差 / 对平检查 / 截止时间归一化（无数据库与框架依赖，可单测）。

口径（README §10 同步，改动需两侧一致）：
- **回退判定**：修订重算后 `direction`（方向）或 `severity`（影响档位 high/medium/low）变化，
  且异常已有复核结论（confirmed/dismissed）→ 原复核归档保留，异常回到待复核；
- **水位差**：同一指标点修订前后 revenue / adr / rooms_sold / occupancy 的差值（补报新增点 before 为 None）；
- **对平**：量价三项（LMDI parts + residual = Δ营收 且当期营收 = 同期明细合计）、
  渠道贡献（Σ渠道当期 = 同期明细合计）、同行对比（Σ同区域门店当期 = 同期明细同区域合计），
  容差 1 元以内视为对平；
- **截止时间**：`YYYY-MM-DD` 归一化为当日 23:59:59；重试同一截止时间幂等，不新增修订。
"""
from __future__ import annotations

from datetime import datetime

RECONCILE_TOLERANCE = 1.0  # 元；浮点与四舍五入误差的容忍上限

WATER_LEVEL_KEYS = ("revenue", "adr", "rooms_sold", "occupancy")


def classify_change(old_direction: str, old_severity: str, new_direction: str, new_severity: str) -> dict:
    """方向 / 影响档位是否变化；任一变化即需回退复核。"""
    direction_changed = old_direction != new_direction
    tier_changed = old_severity != new_severity
    return {
        "direction_changed": direction_changed,
        "tier_changed": tier_changed,
        "reset_required": direction_changed or tier_changed,
    }


def diff_water_levels(before: dict | None, after: dict) -> dict:
    """同一指标点修订前后水位差；before=None（补报新增）时 delta 取 after 值。"""
    deltas: dict[str, float] = {}
    for key in WATER_LEVEL_KEYS:
        new_value = float(after.get(key) or 0.0)
        old_value = float(before.get(key) or 0.0) if before else 0.0
        deltas[key] = round(new_value - old_value, 6)
    return deltas


def reconcile_checks(
    *,
    parts: dict,
    residual: float,
    total_delta: float,
    cur_rev: float,
    channel_current_sum: float,
    peer_current_sum: float,
    detail_current_total: float,
    detail_peer_total: float,
    tolerance: float = RECONCILE_TOLERANCE,
) -> dict:
    """量价三项 / 渠道贡献 / 同行对比 与同期明细对平。

    - volume_price：Σ(adr/occ/rooms 三项) + residual 应等于 total_delta，且当期营收等于明细合计；
    - channel：渠道行当期合计应等于明细合计；
    - peer：同区域门店行当期合计应等于明细同区域合计。
    """
    parts_sum = float(parts.get("adr") or 0.0) + float(parts.get("occ") or 0.0) + float(parts.get("rooms") or 0.0)
    checks = {
        "volume_price": {
            "label": "量价三项",
            "diff": round((parts_sum + float(residual)) - float(total_delta), 6),
            "current_diff": round(float(cur_rev) - float(detail_current_total), 6),
        },
        "channel": {
            "label": "渠道贡献",
            "diff": round(float(channel_current_sum) - float(detail_current_total), 6),
        },
        "peer": {
            "label": "同行对比",
            "diff": round(float(peer_current_sum) - float(detail_peer_total), 6),
        },
    }
    max_abs = 0.0
    for check in checks.values():
        diffs = [abs(check["diff"])] + ([abs(check["current_diff"])] if "current_diff" in check else [])
        check["ok"] = all(value <= tolerance for value in diffs)
        max_abs = max(max_abs, *diffs)
    return {"ok": all(check["ok"] for check in checks.values()), "max_abs_diff": round(max_abs, 6), "checks": checks}


def normalize_cutoff(text: str) -> datetime:
    """数据截止时间归一化：支持 `YYYY-MM-DD`（→ 当日 23:59:59）与 `YYYY-MM-DD[ T]HH:MM[:SS]`。"""
    cleaned = text.strip().replace(" ", "T")
    if len(cleaned) == 10:
        cleaned = f"{cleaned}T23:59:59"
    elif len(cleaned) == 16:
        cleaned = f"{cleaned}:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"非法的数据截止时间: {text}") from exc


def report_dedupe_key(kind: str, period_start: str, period_end: str, revision_id: int | None) -> str:
    """报告幂等键：同一区间 + 同一数据修订重试不新增报告。"""
    return f"{kind}|{period_start}|{period_end}|rev:{revision_id or 0}"
