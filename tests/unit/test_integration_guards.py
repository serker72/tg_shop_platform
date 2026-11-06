"""Unit tests of the integration safety guards (no infrastructure needed)."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from tests.integration._guards import (
    assert_clean_db_compatible,
    assert_same_database,
    assert_test_database,
    assert_test_database_name,
    database_target,
)

PASSWORD = "s3cr3t-guard-password"


def _settings(environment: str, db: str) -> Settings:
    return Settings(
        _env_file=None,
        project_environment=environment,
        postgres={"db": db, "password": PASSWORD},
    )


@pytest.fixture(params=["0", "1"], ids=["local", "require_infra"])
def require_infra(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    if request.param == "1":
        monkeypatch.setenv("REQUIRE_INFRA", "1")
    else:
        monkeypatch.delenv("REQUIRE_INFRA", raising=False)
    return bool(request.param == "1")


def _expected(require_infra: bool) -> type[BaseException]:
    return pytest.fail.Exception if require_infra else pytest.skip.Exception


@pytest.mark.parametrize(
    ("environment", "db", "needle"),
    [
        ("loc", "tg_shop_platform_test", "PROJECT_ENVIRONMENT='loc'"),
        ("test", "tg_shop_platform", "POSTGRES_DB='tg_shop_platform'"),
        ("loc", "tg_shop_platform", "PROJECT_ENVIRONMENT='loc'"),
    ],
)
def test_non_test_settings_are_refused(
    require_infra: bool, environment: str, db: str, needle: str
) -> None:
    with pytest.raises(_expected(require_infra)) as info:
        assert_test_database(_settings(environment, db))
    message = str(info.value)
    assert needle in message
    assert "make env-test" in message
    assert PASSWORD not in message


def test_test_settings_pass(require_infra: bool) -> None:
    assert_test_database(_settings("test", "tg_shop_platform_test"))


def test_dev_defaults_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``.env.test``: the defaults (``loc``, ``tg_shop_platform``) never pass."""
    monkeypatch.delenv("PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("REQUIRE_INFRA", raising=False)
    with pytest.raises(pytest.skip.Exception):
        assert_test_database(Settings(_env_file=None))


def test_current_database_guard_always_fails(require_infra: bool) -> None:
    assert_test_database_name("tg_shop_platform_test")
    with pytest.raises(pytest.fail.Exception, match="tg_shop_platform"):
        assert_test_database_name("tg_shop_platform")


@pytest.mark.parametrize("clash", ["session", "session_factory", "db_connection"])
def test_clean_db_rejects_outer_transaction_fixtures(clash: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="clean_db is incompatible"):
        assert_clean_db_compatible(["clean_db", "engine", clash])


def _url(host: str = "127.0.0.1", port: int = 25432, db: str = "shop_test") -> str:
    return f"postgresql+psycopg://user:{PASSWORD}@{host}:{port}/{db}"


def test_database_target_drops_password() -> None:
    target = database_target(_url())
    assert target == ("127.0.0.1", 25432, "shop_test")
    assert PASSWORD not in repr(target)


def test_same_database_passes() -> None:
    assert_same_database(("127.0.0.1", 25432, "shop_test"), _url())


@pytest.mark.parametrize(
    "url",
    [_url(db="shop"), _url(port=5432), _url(host="postgres")],
    ids=["db", "port", "host"],
)
def test_different_database_fails_without_password(url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="migrated_db verified") as info:
        assert_same_database(("127.0.0.1", 25432, "shop_test"), url)
    assert PASSWORD not in str(info.value)


def test_clean_db_alone_is_allowed() -> None:
    assert_clean_db_compatible(["clean_db", "engine", "migrated_db", "settings"])
