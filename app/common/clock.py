"""时间工具：统一 UTC ISO-8601 输出（Z 结尾，对齐旧 Java Instant.toString()）。"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return iso_z(utc_now())
