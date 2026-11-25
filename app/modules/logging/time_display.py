"""Timezone projection for log displays, including extreme UTC instants."""

from datetime import UTC, datetime

from app.shared_kernel.timezones import get_timezone


def project_for_display(instant: datetime, timezone: str) -> tuple[datetime, bool]:
    """Return the displayed instant and whether local projection fell back to UTC."""
    if instant.utcoffset() is None:
        raise ValueError("Expected an aware datetime")
    zone = get_timezone(timezone)
    try:
        return instant.astimezone(zone), False
    except OverflowError:
        return instant.astimezone(UTC), True
