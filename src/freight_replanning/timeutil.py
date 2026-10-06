"""统一的时间处理：内部一律使用带时区的 ISO 8601 字符串。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    """当前 UTC 时间，秒级精度。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse(ts: str) -> datetime:
    """解析 ISO 时间；缺省时区按 UTC 处理。"""
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def plus_minutes(ts: str, minutes: float) -> str:
    return iso(parse(ts) + timedelta(minutes=minutes))
