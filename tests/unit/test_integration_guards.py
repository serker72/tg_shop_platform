"""Unit tests of the integration safety guards (no infrastructure needed)."""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 - patched at the CLI boundary, never executes in tests
from pathlib import Path

import pytest
from aio_pika.connection import make_url as make_amqp_url
from faststream.rabbit import RabbitBroker
from yarl import URL

from app.core.config import Settings
from tests.integration import _guards
from tests.integration._guards import (
    assert_clean_db_compatible,
    assert_same_database,
    assert_test_database,
    assert_test_database_name,
    database_target,
    guarded_rabbit_broker,
    guarded_rabbit_url,
)

PASSWORD = "s3cr3t-guard-password"


@pytest.mark.parametrize(
    "case",
    [
        "match",
        "host",
        "port",
        "vhost",
        "no-metadata",
        "bad-mount",
        "wildcard",
        "absent-vhost",
        "padded-vhost",
        "trimmed-vhost",
        "malformed-json",
        "invalid-row",
    ],
)
def test_rabbit_guard_matches_independent_local_metadata(
    case: str, require_infra: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = "a" * 12
    labels = {
        "com.docker.compose.project": "shop_test",
        "com.docker.compose.service": "rabbitmq",
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.project.working_dir": str(Path(_guards.__file__).resolve().parents[2]),
    }
    mount = {
        "Type": "bind",
        "Source": "/data/shop/rabbitmq" if case == "bad-mount" else "/data/shop_test/rabbitmq",
        "Destination": "/var/lib/rabbitmq",
    }
    host_ip = "0.0.0.0" if case == "wildcard" else "127.0.0.1"  # noqa: S104 - mock metadata
    mapping = {"HostIp": host_ip, "HostPort": "33678"}

    provisioned = " shop " if case in {"padded-vhost", "trimmed-vhost"} else "shop/vhost"

    def output(command: list[str], **_: object) -> bytes:
        assert command[:3] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
        args = command[3:]
        if args[0] == "ps":
            return b"" if case == "no-metadata" else f"{container}\n".encode()
        if args[0] == "inspect":
            assert args[-1] == container and '"5672/tcp"' in args[2]
            return "".join(
                f"{json.dumps(item)}\n" for item in (labels, [mount], [mapping])
            ).encode()
        assert args[:2] == ["exec", container]
        if args[2:] == ["printenv", "RABBITMQ_DEFAULT_VHOST"]:
            return f"{provisioned}\n".encode()
        assert args[2:] == ["rabbitmqctl", "-q", "--formatter=json", "list_vhosts", "name"]
        if case == "malformed-json":
            return b"not JSON"
        names: list[dict[str, object]] = [{"name": "/"}, {"name": "shop"}]
        if case != "absent-vhost":
            names.append({"name": provisioned})
        if case == "invalid-row":
            names.append({"name": 1})
        return json.dumps(names).encode()

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "check_output", output)
    requested = {"vhost": "/", "trimmed-vhost": "shop"}.get(case, provisioned)
    settings = _settings("test", "shop_test")
    rabbit = settings.rabbitmq.model_copy(
        update={
            "host": "rabbitmq" if case == "host" else "127.0.0.1",
            "port": 5672 if case == "port" else 33678,
            "vhost": requested,
        }
    )
    settings = settings.model_copy(update={"rabbitmq": rabbit})
    if case == "match":
        assert guarded_rabbit_url(settings).endswith("@127.0.0.1:33678/shop%2Fvhost")
    elif case == "padded-vhost":
        assert guarded_rabbit_url(settings).endswith("@127.0.0.1:33678/%20shop%20")
    elif case in {"host", "port", "vhost", "trimmed-vhost"}:
        with pytest.raises(pytest.fail.Exception, match="do not match the isolated RabbitMQ"):
            guarded_rabbit_url(settings)
    else:
        with pytest.raises(_expected(require_infra), match="RabbitMQ metadata unavailable"):
            guarded_rabbit_url(settings)


@pytest.mark.parametrize(
    "effective",
    [
        ("amqps", "127.0.0.1", 33678, "/"),
        ("amqp", "other.invalid", 33678, "/"),
        ("amqp", "127.0.0.1", 33679, "/"),
        ("amqp", "127.0.0.1", 33678, "other"),
        None,
    ],
    ids=["scheme", "host", "port", "vhost", "parser-error"],
)
def test_rabbit_guard_rejects_effective_destination_mismatch(
    monkeypatch: pytest.MonkeyPatch, effective: tuple[str, str, int, str] | None
) -> None:
    expected = ("127.0.0.1", 33678, "/")
    settings = _settings("test", "shop_test")
    rabbit = settings.rabbitmq.model_copy(
        update={
            "host": expected[0],
            "port": expected[1],
            "vhost": expected[2],
            "password": settings.postgres.password,
        }
    )
    settings = settings.model_copy(update={"rabbitmq": rabbit})
    monkeypatch.setattr(_guards, "_local_test_rabbit_target", lambda: expected)
    assert guarded_rabbit_url(settings) == settings.rabbitmq_url

    seen: list[str] = []

    def parse(dsn: str) -> tuple[str, str, int, str]:
        seen.append(dsn)
        if effective is None:
            raise ValueError(dsn)
        return effective

    monkeypatch.setattr(_guards, "_effective_amqp_target", parse)
    with pytest.raises(pytest.fail.Exception, match="effective AMQP destination") as error:
        guarded_rabbit_url(settings)
    assert PASSWORD not in str(error.value)
    assert seen == [settings.rabbitmq_url]
    _assert_guard_failure_is_redacted(error, "guarded_rabbit_url")


def _assert_guard_failure_is_redacted(
    error: pytest.ExceptionInfo[BaseException], function: str
) -> None:
    assert PASSWORD not in str(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    entry = next(entry for entry in error.traceback if entry.name == function)
    assert PASSWORD not in repr(entry.locals)
    assert "amqp://" not in repr(entry.locals)


@pytest.mark.parametrize("case", ["match", "mismatch", "missing"])
def test_prepared_rabbit_broker_guard(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ("127.0.0.1", 33678, "/")
    settings = _settings("test", "shop_test")
    broker = RabbitBroker(f"amqp://test:{PASSWORD}@127.0.0.1:33678/%2F")
    monkeypatch.setattr(_guards, "_local_test_rabbit_target", lambda: expected)
    if case == "match":
        assert guarded_rabbit_broker(broker, settings) is broker
        return

    seen: list[str | URL] = []

    def parse(dsn: str | URL) -> tuple[str, str, int, str]:
        seen.append(dsn)
        return "amqp", "127.0.0.1", 33679, "/"

    monkeypatch.setattr(_guards, "_effective_amqp_target", parse)
    if case == "missing":
        monkeypatch.setattr(broker, "_connection_kwargs", {})
    with pytest.raises(pytest.fail.Exception, match="prepared AMQP destination") as error:
        guarded_rabbit_broker(broker, settings)
    assert len(seen) == int(case == "mismatch")
    if seen:
        assert make_amqp_url(seen[0]).port == expected[1]
    _assert_guard_failure_is_redacted(error, "guarded_rabbit_broker")


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
