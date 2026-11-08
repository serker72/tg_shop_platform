"""``python -m app.entrypoints.cli.setup``: exit codes without a database."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pydantic
import pytest

from app.entrypoints.cli import setup
from app.shared_kernel.domain.exceptions import ConfigurationError


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.events.append(("error", event, kw))


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    logger = _Logger()
    monkeypatch.setattr(setup, "logger", logger)
    monkeypatch.setattr(setup, "configure_logging", lambda level: None)
    return logger


def _validation_error() -> pydantic.ValidationError:
    class _Model(pydantic.BaseModel):
        value: int

    try:
        _Model.model_validate({"value": "x"})
    except pydantic.ValidationError as error:
        return error
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConfigurationError("bad"), setup.EXIT_CONFIG),
        (ConfigurationError("POSTGRES_PASSWORD is not configured"), setup.EXIT_CONFIG),
        (ValueError("arbitrary"), setup.EXIT_ERROR),
        (_validation_error(), setup.EXIT_CONFIG),
        (RuntimeError("boom"), setup.EXIT_ERROR),
    ],
)
def test_exit_code_for(error: BaseException, code: int) -> None:
    assert setup.exit_code_for(error) == code


def test_main_settings_error_exit_2(monkeypatch: pytest.MonkeyPatch, log: _Logger) -> None:
    def broken() -> Any:
        raise _validation_error()

    monkeypatch.setattr(setup, "get_settings", broken)
    assert setup.main([]) == setup.EXIT_CONFIG
    level, event, kw = log.events[-1]
    assert (level, event, kw["exit_code"]) == ("error", "platform_setup_failed", 2)
    assert "traceback" not in kw


def test_main_missing_password_exit_2(monkeypatch: pytest.MonkeyPatch, log: _Logger) -> None:
    """Real ``run``: the password check fires before ``build_engine`` (no database)."""
    settings = SimpleNamespace(postgres=SimpleNamespace(password=None))
    monkeypatch.setattr(setup, "get_settings", lambda: settings)

    def no_engine(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_engine must not be called")

    monkeypatch.setattr(setup, "build_engine", no_engine)
    assert setup.main([]) == setup.EXIT_CONFIG
    kw = log.events[-1][2]
    assert kw["error_type"] == "ConfigurationError"
    assert kw["error"] == "POSTGRES_PASSWORD is not configured"
    assert "traceback" not in kw


def test_main_arbitrary_value_error_exit_1_with_traceback(
    monkeypatch: pytest.MonkeyPatch, log: _Logger
) -> None:
    monkeypatch.setattr(setup, "get_settings", lambda: object())

    async def run(settings: Any) -> bool:
        raise ValueError("unexpected")

    monkeypatch.setattr(setup, "run", run)
    assert setup.main([]) == setup.EXIT_ERROR
    kw = log.events[-1][2]
    assert kw["exit_code"] == 1
    assert "ValueError: unexpected" in kw["traceback"]


def test_main_unexpected_error_exit_1_with_traceback(
    monkeypatch: pytest.MonkeyPatch, log: _Logger
) -> None:
    monkeypatch.setattr(setup, "get_settings", lambda: object())

    async def run(settings: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(setup, "run", run)
    assert setup.main([]) == setup.EXIT_ERROR
    kw = log.events[-1][2]
    assert kw["error_type"] == "RuntimeError"
    assert "RuntimeError: boom" in kw["traceback"]


@pytest.mark.parametrize("inserted", [True, False])
def test_main_success(monkeypatch: pytest.MonkeyPatch, log: _Logger, inserted: bool) -> None:
    monkeypatch.setattr(setup, "get_settings", lambda: object())

    async def run(settings: Any) -> bool:
        return inserted

    monkeypatch.setattr(setup, "run", run)
    assert setup.main([]) == setup.EXIT_OK
    assert log.events[-1] == ("info", "platform_setup_done", {"inserted": inserted})


def test_main_rejects_unknown_args() -> None:
    with pytest.raises(SystemExit) as caught:
        setup.main(["--bogus"])
    assert caught.value.code == 2
