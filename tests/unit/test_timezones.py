import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.bot_manager.models import Customer
from app.modules.iam.models import Tenant, User
from app.shared_kernel.timezones import (
    effective_customer_timezone,
    effective_staff_timezone,
    format_dt,
    get_timezone,
    search_timezones,
    suggest_timezones_by_time,
    to_utc,
    validate_timezone,
)

pytestmark = pytest.mark.req("R-II.17")


def test_timezone_settings_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    assert Settings(_env_file=None).default_timezone == "UTC"
    env = tmp_path / ".env"
    env.write_text("DEFAULT_TIMEZONE=Europe/Riga\n")
    assert Settings().default_timezone == "Europe/Riga"
    env.write_text("DEFAULT_TIMEZONE=Mars/Olympus\n")
    with pytest.raises(ValidationError, match="default_timezone"):
        Settings()
    monkeypatch.setenv("DEFAULT_TIMEZONE", "+02:00")
    with pytest.raises(ValidationError, match="IANA"):
        Settings()


@pytest.mark.parametrize("key", ["+02:00", "UTC+2", "Mars/Olympus", "localtime", "../UTC", "utc"])
def test_invalid_iana_ids(key: str) -> None:
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone(key)


def test_utc_and_effective_fallbacks() -> None:
    assert validate_timezone("UTC") == "UTC"
    assert effective_staff_timezone(None) == "UTC"
    assert effective_staff_timezone(None, "Europe/Riga") == "Europe/Riga"
    assert effective_staff_timezone("Asia/Tokyo", "Europe/Riga") == "Asia/Tokyo"
    assert effective_customer_timezone("Asia/Tokyo", "Europe/Riga") == "Asia/Tokyo"
    assert effective_customer_timezone(None, "Europe/Riga") == "Europe/Riga"
    assert effective_customer_timezone(None, None, "Asia/Tokyo") == "Asia/Tokyo"
    assert effective_customer_timezone(None, None) == "UTC"


def test_conversion_crosses_midnight() -> None:
    instant = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    wall_time = datetime(2026, 1, 2, 1, 30)
    assert format_dt(instant, "UTC") == "2026-01-01 23:30:00 UTC"
    assert format_dt(instant, "Europe/Riga", "%Y-%m-%d %H:%M") == "2026-01-02 01:30"
    assert to_utc(wall_time, "Europe/Riga") == instant
    with pytest.raises(ValueError, match="aware"):
        format_dt(wall_time, "UTC")


def test_dst_gap_rejected_and_fold_must_be_explicit() -> None:
    key = "America/New_York"
    zone = get_timezone(key)
    gap = datetime(2026, 3, 8, 2, 30)
    fold = datetime(2026, 11, 1, 1, 30)
    for wall in (gap, fold, fold.replace(fold=1)):
        with pytest.raises(ValueError, match="Ambiguous or nonexistent"):
            to_utc(wall, key)
    for choice in (0, 1):
        with pytest.raises(ValueError, match="Nonexistent"):
            to_utc(gap.replace(tzinfo=zone, fold=choice), key)
        assert to_utc(fold.replace(tzinfo=zone, fold=choice), key) == datetime(
            2026, 11, 1, 5 + choice, 30, tzinfo=UTC
        )
    with pytest.raises(ValueError, match="matching"):
        to_utc(fold.replace(tzinfo=get_timezone("UTC")), key)


def test_suggestions_date_line_fixed_fallback_and_quarter_hour() -> None:
    instant = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    assert "Pacific/Kiritimati" in suggest_timezones_by_time("13:30", instant)
    assert "Pacific/Honolulu" in suggest_timezones_by_time("13:30", instant)
    assert "Etc/GMT+12" in suggest_timezones_by_time("11:30", instant)
    assert "Asia/Kathmandu" in suggest_timezones_by_time("05:15", instant)
    assert "Asia/Kathmandu" in suggest_timezones_by_time("05:30", instant)
    assert "Pacific/Chatham" in suggest_timezones_by_time("13:15", instant)


@pytest.mark.parametrize(
    ("wall_time", "key"), [("05:44", "Asia/Kathmandu"), ("13:44", "Pacific/Chatham")]
)
def test_suggestions_include_quarter_hour_zones_across_rounding_boundary(
    wall_time: str, key: str
) -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    assert key in suggest_timezones_by_time(wall_time, instant)
    hour = wall_time[:2]
    assert key not in suggest_timezones_by_time(f"{hour}:14", instant)


@pytest.mark.parametrize("text", ["1:00", "24:00", "03:60", "12:00:00", "１２:００"])
def test_suggestions_validate_hhmm(text: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        suggest_timezones_by_time(text, datetime(2026, 1, 1, tzinfo=UTC))


def test_search_city_region_and_seasonal_offset() -> None:
    winter = datetime(2026, 1, 1, tzinfo=UTC)
    summer = datetime(2026, 7, 1, tzinfo=UTC)
    assert search_timezones("new YORK", now_utc=winter) == [
        ("America/New_York", timedelta(hours=-5))
    ]
    assert ("America/New_York", timedelta(hours=-4)) in search_timezones("america", now_utc=summer)
    assert search_timezones("eUrOpE/rIgA", now_utc=winter) == [("Europe/Riga", timedelta(hours=2))]
    assert search_timezones("+2", now_utc=winter) == search_timezones("+02:00", now_utc=winter)
    assert ("Europe/Riga", timedelta(hours=2)) in search_timezones("+2", now_utc=winter)
    assert ("Europe/Riga", timedelta(hours=3)) in search_timezones("+3", now_utc=summer)
    assert all(offset == timedelta(hours=2) for _, offset in search_timezones("+2", now_utc=winter))
    assert ("Etc/GMT-2", timedelta(hours=2)) in search_timezones("+2", now_utc=winter)
    assert ("Etc/GMT+5", timedelta(hours=-5)) in search_timezones("-5", now_utc=winter)
    assert all(
        offset == timedelta(hours=-5) for _, offset in search_timezones("-5", now_utc=winter)
    )
    assert ("Asia/Kathmandu", timedelta(hours=5, minutes=45)) in search_timezones(
        " +05:45 ", now_utc=winter
    )
    assert search_timezones("+24:00", now_utc=winter) == []
    assert search_timezones("-05:60", now_utc=winter) == []


def test_tzdata_without_system_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONTZPATH", "")
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from zoneinfo import TZPATH; assert not TZPATH; "
            "from app.shared_kernel.timezones import get_timezone, search_timezones; "
            "assert get_timezone('Europe/Riga').key == 'Europe/Riga'; "
            "assert search_timezones('Kathmandu')",
        ],
        check=True,
    )


def test_packaged_catalog_and_rules_ignore_runtime_tzpath(tmp_path: Path) -> None:
    utc = files("tzdata.zoneinfo").joinpath("UTC").read_bytes()
    for key in ("Custom/Office", "Europe/Riga"):
        path = tmp_path / key
        path.parent.mkdir()
        path.write_bytes(utc)
    code = """
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from app.shared_kernel.timezones import get_timezone, search_timezones, suggest_timezones_by_time
now = datetime(2026, 1, 1, tzinfo=UTC)
assert ZoneInfo('Custom/Office').utcoffset(now) == timedelta(0)
assert ZoneInfo('Europe/Riga').utcoffset(now) == timedelta(0)
assert search_timezones('Custom/Office', now_utc=now) == []
assert search_timezones('Europe/Riga', now_utc=now) == [('Europe/Riga', timedelta(hours=2))]
assert 'Europe/Riga' in suggest_timezones_by_time('02:00', now)
try:
    get_timezone('Custom/Office')
except ValueError:
    pass
else:
    raise AssertionError('Custom/Office must be rejected')
assert get_timezone('Europe/Riga') is get_timezone('Europe/Riga')
"""
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env={"PYTHONTZPATH": str(tmp_path)}, check=True
    )


def test_to_utc_rejects_mismatched_aware_rules() -> None:
    with files("tzdata.zoneinfo").joinpath("UTC").open("rb") as tzif:
        shadow = ZoneInfo.from_file(tzif, key="Europe/Riga")
    with pytest.raises(ValueError, match="packaged IANA rules"):
        to_utc(datetime(2026, 1, 1, tzinfo=shadow), "Europe/Riga")


def test_model_validates_timezone_assignment() -> None:
    tenant = Tenant(name="Example", slug="example")
    user = User(telegram_user_id=101)
    customer = Customer(tenant_id=uuid.uuid4(), telegram_user_id=101)
    for model in (tenant, user, customer):
        model.timezone = "Europe/Riga"
        assert model.timezone == "Europe/Riga"
        with pytest.raises(ValueError, match="IANA"):
            model.timezone = "+02:00"
    user.timezone = customer.timezone = None
    assert user.timezone is None and customer.timezone is None
