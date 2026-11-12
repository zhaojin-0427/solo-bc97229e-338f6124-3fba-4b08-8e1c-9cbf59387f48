"""时间解析与 CSV 表头工具。

对外时刻均为 UTC 整秒；接受的字符串形式：
  - 2026-01-02T03:04:05Z
  - 2026-01-02T03:04:05+00:00 / 带其他偏移（换算为 UTC）
  - 2026-01-02 03:04:05（按 UTC 处理）
拒绝非整秒（小数秒）。
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from typing import Iterable

# 中英文表头别名 -> 规范字段名
HEADER_ALIASES = {
    # 归档
    "机组": "turbine",
    "turbine": "turbine",
    "turbine_id": "turbine",
    "测量时刻": "measured_at",
    "measured_at": "measured_at",
    "可用时刻": "available_at",
    "available_at": "available_at",
    "温度": "temperature",
    "temperature": "temperature",
    # 运维样本
    "样本号": "sample_id",
    "sample_id": "sample_id",
    "id": "sample_id",
    "预测时刻": "predicted_at",
    "predicted_at": "predicted_at",
    "故障标签": "fault_label",
    "fault_label": "fault_label",
    "label": "fault_label",
}


class TimeParseError(ValueError):
    pass


def parse_utc_second(value: str, field: str = "time") -> int:
    """把时间字符串解析为 UTC 纪元秒，拒绝小数秒与无法识别的格式。"""
    if value is None:
        raise TimeParseError(f"{field} 为空")
    text = str(value).strip()
    if not text:
        raise TimeParseError(f"{field} 为空")
    if "." in text or "," in text:
        raise TimeParseError(f"{field} 必须是 UTC 整秒，不允许小数秒: {text}")

    iso = text.replace("Z", "+00:00").replace(" ", "T", 1) if " " in text else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise TimeParseError(f"{field} 时间格式无法识别: {text}") from exc

    if dt.tzinfo is None:
        # 题目约定时刻均为 UTC，裸时间按 UTC 处理
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond != 0:
        raise TimeParseError(f"{field} 必须是 UTC 整秒: {text}")
    return int(dt.timestamp())


def format_utc_second(epoch_second: int | None) -> str:
    if epoch_second is None:
        return ""
    return datetime.fromtimestamp(int(epoch_second), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def normalize_headers(fieldnames: Iterable[str] | None) -> list[str]:
    if not fieldnames:
        raise csv.Error("CSV 缺少表头")
    normalized: list[str] = []
    for name in fieldnames:
        key = (name or "").strip().lower()
        canonical = HEADER_ALIASES.get(key)
        if canonical is None:
            raise csv.Error(f"未知表头列: {name!r}")
        normalized.append(canonical)
    return normalized


def require_columns(columns: list[str], required: list[str], what: str) -> None:
    missing = [c for c in required if c not in columns]
    if missing:
        raise csv.Error(f"{what}缺少必需列: {', '.join(missing)}")
    if len(set(columns)) != len(columns):
        raise csv.Error(f"{what}存在重复列")
