"""时间工具：服务内部统一使用带时区的 UTC 时间，对外序列化为 ISO-8601 'Z' 形式。"""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("时间必须携带时区偏移，例如 2026-09-28T10:00:00+08:00")
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"无法解析的时间格式: {s!r}，需为 ISO-8601，例如 2026-09-28T10:00:00+08:00") from exc
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区偏移: {s!r}")
    return dt
