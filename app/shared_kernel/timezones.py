"""IANA timezone IDs at application boundaries; persisted timestamps stay in UTC."""

import re
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.resources import files
from typing import cast
from zoneinfo import ZoneInfo

_OFFSET_QUERY = re.compile(r"([+-])([0-9]{1,2})(?::([0-5][0-9]))?")
_DAY_SECONDS = 24 * 60 * 60
_HALF_HOUR_SECONDS = 30 * 60

# Stable, human-friendly choices shown before the IANA search.
TIMEZONE_PRESETS: tuple[str, ...] = (
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Moscow",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Shanghai",
    "America/New_York",
)


def timezone_page(
    query: str, page: int, *, page_size: int = 10, now_utc: datetime | None = None
) -> tuple[list[tuple[str, timedelta]], bool]:
    """Return one bounded page of IANA search results and whether another page exists."""
    if page < 0 or page_size < 1:
        raise ValueError("Invalid timezone page")
    rows = search_timezones(query, now_utc=now_utc)
    start = page * page_size
    return rows[start : start + page_size], start + page_size < len(rows)


@cache
def _timezone_ids() -> tuple[str, ...]:
    return tuple(sorted(files("tzdata").joinpath("zones").read_text(encoding="utf-8").splitlines()))


@cache
def _zone_from_package(timezone_id: str) -> ZoneInfo:
    resource = files("tzdata.zoneinfo").joinpath(*timezone_id.split("/"))
    with resource.open("rb") as tzif:
        return ZoneInfo.from_file(tzif, key=timezone_id)


def get_timezone(timezone_id: str) -> ZoneInfo:
    """Resolve a case-sensitive IANA ID using only the locked tzdata distribution."""
    if timezone_id not in _timezone_ids():
        raise ValueError(f"Invalid IANA timezone ID: {timezone_id!r}")
    return _zone_from_package(timezone_id)


def validate_timezone(timezone_id: str) -> str:
    get_timezone(timezone_id)
    return timezone_id


def effective_staff_timezone(user_timezone: str | None, default_timezone: str = "UTC") -> str:
    return validate_timezone(user_timezone if user_timezone is not None else default_timezone)


def effective_customer_timezone(
    customer_timezone: str | None, tenant_timezone: str | None, default_timezone: str = "UTC"
) -> str:
    return effective_staff_timezone(
        customer_timezone if customer_timezone is not None else tenant_timezone, default_timezone
    )


def _require_aware(dt: datetime) -> None:
    if dt.utcoffset() is None:
        raise ValueError("Expected an aware datetime")


def format_dt(dt: datetime, timezone_id: str, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    """Render an aware instant in an IANA timezone."""
    _require_aware(dt)
    return dt.astimezone(get_timezone(timezone_id)).strftime(fmt)


def _roundtrip_utc(local: datetime, zone: ZoneInfo) -> datetime | None:
    utc = local.astimezone(UTC)
    result = utc.astimezone(zone)
    if result.replace(tzinfo=None) == local.replace(tzinfo=None) and result.fold == local.fold:
        return utc
    return None


def to_utc(local_dt: datetime, timezone_id: str) -> datetime:
    """Convert wall time; reject gaps and folds unless a valid ZoneInfo fold is explicit."""
    zone = get_timezone(timezone_id)
    if local_dt.tzinfo is not None:
        if not isinstance(local_dt.tzinfo, ZoneInfo) or local_dt.tzinfo.key != zone.key:
            raise ValueError("Expected a naive datetime or matching aware ZoneInfo datetime")
        trusted_local = local_dt.replace(tzinfo=zone)
        if trusted_local.utcoffset() != local_dt.utcoffset():
            raise ValueError("Aware datetime offset does not match packaged IANA rules")
        result = _roundtrip_utc(trusted_local, zone)
        if result is None:
            raise ValueError("Nonexistent local time or invalid fold")
        return result

    candidates = {
        utc
        for fold in (0, 1)
        if (utc := _roundtrip_utc(local_dt.replace(tzinfo=zone, fold=fold), zone)) is not None
    }
    if len(candidates) != 1:
        raise ValueError("Ambiguous or nonexistent local time")
    return candidates.pop()


def _offset_at(instant: datetime, timezone_id: str) -> timedelta:
    # ZoneInfo always supplies an offset for a concrete datetime.
    return cast(timedelta, instant.astimezone(get_timezone(timezone_id)).utcoffset())


def search_timezones(query: str, *, now_utc: datetime | None = None) -> list[tuple[str, timedelta]]:
    """Find IANA IDs by name or exact current UTC offset (e.g. +2, -5, +05:45)."""
    instant = datetime.now(UTC) if now_utc is None else now_utc
    _require_aware(instant)
    instant = instant.astimezone(UTC)
    query = query.strip()
    numeric = _OFFSET_QUERY.fullmatch(query)
    if numeric:
        sign, hours, minutes = numeric.groups()
        if int(hours) >= 24:
            return []
        offset = timedelta(hours=int(hours), minutes=int(minutes or "0"))
        offset = -offset if sign == "-" else offset
        return [(key, offset) for key in _timezone_ids() if _offset_at(instant, key) == offset]
    if query.startswith(("+", "-")):
        return []
    needle = " ".join(query.casefold().replace("_", " ").split())
    return [
        (key, _offset_at(instant, key))
        for key in _timezone_ids()
        if needle in key.casefold().replace("_", " ")
    ]


def _half_hour_bucket(seconds: float) -> int:
    # Nearest half hour, ties toward +infinity (consistent across the date line).
    return int((seconds + _HALF_HOUR_SECONDS // 2) // _HALF_HOUR_SECONDS)


def suggest_timezones_by_time(time_str: str, now_utc: datetime) -> list[str]:
    """Suggest IANA IDs by current wall time, ignoring the unknown local calendar date.

    Round only the requested offset, then compare with real offsets modulo 24 hours
    within 15 minutes. Prefer regional zones over fixed IDs at the same signed offset.
    """
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", time_str):
        raise ValueError("Expected HH:MM (00:00 through 23:59)")
    _require_aware(now_utc)
    instant = now_utc.astimezone(UTC)
    hour, minute = map(int, time_str.split(":"))
    target = _half_hour_bucket(60 * (60 * (hour - instant.hour) + minute - instant.minute))
    target_seconds = target * _HALF_HOUR_SECONDS
    tolerance = _HALF_HOUR_SECONDS // 2
    matches = []
    for key in _timezone_ids():
        if "/" not in key or key.startswith("Etc/"):
            continue
        offset = _offset_at(instant, key).total_seconds()
        delta = (offset - target_seconds) % _DAY_SECONDS
        if min(delta, _DAY_SECONDS - delta) <= tolerance:
            matches.append((offset, key))

    result = [key for _, key in matches]
    for hours in range(-12, 15):
        offset = hours * 3600
        if (offset - target_seconds) % _DAY_SECONDS:
            continue
        if any(abs(real_offset - offset) <= tolerance for real_offset, _ in matches):
            continue
        # The Etc/GMT name uses POSIX signs, opposite to the actual UTC offset.
        fixed = "UTC" if hours == 0 else f"Etc/GMT{-hours:+d}"
        if fixed in _timezone_ids():
            result.append(fixed)
    return result
