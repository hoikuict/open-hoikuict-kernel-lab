from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


DEFAULT_LOCAL_TIMEZONE = "Asia/Tokyo"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_timezone(name: str | None = None):
    timezone_name = (name or DEFAULT_LOCAL_TIMEZONE).strip() or DEFAULT_LOCAL_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        if timezone_name in {"Asia/Tokyo", "JST"}:
            return timezone(timedelta(hours=9), name="JST")
        return timezone.utc


def ensure_utc_from_local(value: datetime | None, timezone_name: str | None = None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_local_timezone(timezone_name))
    return value.astimezone(timezone.utc)
