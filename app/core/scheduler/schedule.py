"""Fire times of periodic tasks: ``daily_at(HH:MM UTC)`` and ``every(seconds)``.

The planned fire time (slot) - not ``now()`` - identifies a run: replicas with slightly
different clocks compute the same slot and race for the same Redis lock key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

_HHMM: Final = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_SECOND: Final = timedelta(seconds=1)
#: Extra lock lifetime over the period (capped): a daily task keeps its lock for 25 h.
LOCK_GRACE_MAX_SECONDS: Final = 3600
DAY_SECONDS: Final = 86400


def parse_hhmm(value: str) -> tuple[int, int]:
    """``"03:00"`` -> ``(3, 0)``; anything else -> ``ValueError``."""
    match = _HHMM.match(value.strip())
    if match is None:
        raise ValueError(f"expected HH:MM (UTC, 00:00-23:59), got {value!r}")
    return int(match.group(1)), int(match.group(2))


def to_utc(moment: datetime) -> datetime:
    """Aware ``moment`` in UTC; naive datetimes are rejected (ambiguous slot)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime: pass an aware (UTC) datetime")
    return moment.astimezone(UTC)


def slot_label(slot: datetime) -> str:
    """ISO UTC label of a slot with a ``Z`` suffix: ``2026-11-15T03:00:00Z``."""
    return to_utc(slot).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


@dataclass(frozen=True, slots=True)
class Schedule:
    """Fires at ``epoch + offset + k * period`` (UTC), ``0 <= offset < period``."""

    period_seconds: int
    offset_seconds: int = 0

    def __post_init__(self) -> None:
        if self.period_seconds < 1:
            raise ValueError("period_seconds must be >= 1")
        if not 0 <= self.offset_seconds < self.period_seconds:
            raise ValueError("offset_seconds must be within [0, period_seconds)")

    @classmethod
    def daily_at(cls, value: str) -> Schedule:
        """Every day at ``HH:MM`` UTC."""
        hour, minute = parse_hhmm(value)
        return cls(DAY_SECONDS, hour * 3600 + minute * 60)

    @classmethod
    def every(cls, seconds: int) -> Schedule:
        """Every ``seconds`` aligned to the Unix epoch (``every(3600)`` -> on the hour)."""
        return cls(seconds)

    def previous_fire(self, now: datetime) -> datetime:
        """Latest planned fire time ``<= now`` (UTC)."""
        elapsed = (to_utc(now) - _EPOCH) // _SECOND
        slots = (elapsed - self.offset_seconds) // self.period_seconds
        return _EPOCH + timedelta(seconds=slots * self.period_seconds + self.offset_seconds)

    @property
    def lock_ttl_seconds(self) -> int:
        """Lock TTL ``>= period``: the period plus ``min(period, 1 h)`` (daily -> 25 h)."""
        return self.period_seconds + min(self.period_seconds, LOCK_GRACE_MAX_SECONDS)
