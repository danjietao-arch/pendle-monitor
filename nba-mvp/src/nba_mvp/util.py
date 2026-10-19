from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone


class DataError(Exception):
    """A source payload could not be parsed. Callers must not fill zeros."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_unix(float(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _from_unix(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DataError(f"无法解析时间: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _from_unix(num: float) -> datetime:
    if num > 10_000_000_000:
        num = num / 1000.0
    return datetime.fromtimestamp(num, timezone.utc)


def floor_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def hour_stamp(dt: datetime) -> str:
    return isoformat(floor_hour(dt))


def hour_end(as_of_hour: str) -> datetime:
    return parse_time(as_of_hour) + timedelta(hours=1)


def previous_hour(as_of_hour: str) -> str:
    return isoformat(parse_time(as_of_hour) - timedelta(hours=1))


def filename_stamp(as_of_hour: str) -> str:
    """Windows-safe file stamp. Colons are not used."""
    return parse_time(as_of_hour).strftime("%Y%m%dT%H%M%SZ")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().casefold()


def normalize_season(text: str) -> str | None:
    match = re.search(r"(20\d{2})\s*[-–]\s*(\d{2})", text or "")
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def season_start_year(season: str) -> int:
    return int(str(season)[:4])


def is_synthetic_season(season: str) -> bool:
    return season_start_year(season) >= 2090
