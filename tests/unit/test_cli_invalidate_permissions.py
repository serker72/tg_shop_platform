"""``python -m app.entrypoints.cli.invalidate_permissions``: best-effort cache bump."""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.entrypoints.cli import invalidate_permissions as cli
from app.modules.iam.cache import GLOBAL_GENERATION_KEY

pytestmark = pytest.mark.req("R-II.2")


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event))


class _Redis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.incremented: list[str] = []
        self.closed = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise RedisConnectionError("down")
        self.incremented.append(key)
        return len(self.incremented)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    logger = _Logger()
    monkeypatch.setattr(cli, "logger", logger)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(cli, "get_settings", lambda: object())
    return logger


@pytest.mark.parametrize("fail", [False, True], ids=["ok", "redis_down"])
def test_bumps_global_generation_best_effort(
    monkeypatch: pytest.MonkeyPatch, log: _Logger, fail: bool
) -> None:
    redis = _Redis(fail=fail)
    monkeypatch.setattr(cli, "build_redis", lambda settings: redis)
    assert cli.main([]) == cli.EXIT_OK
    assert redis.closed
    if fail:
        assert redis.incremented == []
        assert log.events == [("warning", "permission_cache_invalidate_skipped")]
    else:
        assert redis.incremented == [GLOBAL_GENERATION_KEY]
        assert log.events == [("info", "permission_cache_invalidated")]


def test_configuration_error_is_a_warning(monkeypatch: pytest.MonkeyPatch, log: _Logger) -> None:
    def broken(settings: Any) -> Any:
        raise ValueError("bad url")

    monkeypatch.setattr(cli, "build_redis", broken)
    assert cli.main([]) == cli.EXIT_OK
    assert log.events == [("warning", "permission_cache_invalidate_skipped")]
