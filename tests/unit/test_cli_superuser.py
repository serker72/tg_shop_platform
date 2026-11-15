"""``python -m app.entrypoints.cli.superuser``: argument parsing and exit codes (no DB)."""

from __future__ import annotations

import argparse
import uuid
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest

from app.entrypoints.cli import _common
from app.entrypoints.cli import superuser as cli
from app.modules.iam.superuser import LAST_SUPERUSER, SuperuserResult
from app.shared_kernel.domain.exceptions import ConfigurationError, ValidationError


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self.events.append(("error", event, kw))


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    logger = _Logger()
    monkeypatch.setattr(cli, "logger", logger)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(postgres=SimpleNamespace(password=None))
    )
    return logger


def _fake_run(result: SuperuserResult | None = None, error: Exception | None = None) -> Any:
    async def run(settings: Any, args: argparse.Namespace) -> SuperuserResult:
        if error is not None:
            raise error
        assert result is not None
        return result

    return run


def _result(*, cache_invalidated: bool = True) -> SuperuserResult:
    return SuperuserResult(
        telegram_user_id=42, user_id=uuid.uuid4(), changed=True, cache_invalidated=cache_invalidated
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["create"],
        ["create", "--telegram-id", "abc"],
        ["create", "--telegram-id", "0"],
        ["create", "--telegram-id", "-5"],
        ["create", "--telegram-id", "1", "--force"],
        ["revoke"],
        ["revoke", "--telegram-id", "1", "--username", "x"],
        ["delete", "--telegram-id", "1"],
    ],
)
def test_bad_arguments_exit_2(argv: list[str], log: _Logger) -> None:
    assert cli.main(argv) == _common.EXIT_CONFIG
    assert log.events == []


def test_help_exit_0(log: _Logger) -> None:
    assert cli.main(["--help"]) == _common.EXIT_OK


def test_parser_values() -> None:
    parser = cli.build_parser()
    create = parser.parse_args(["create", "--telegram-id", "123", "--username", "@boss"])
    assert (create.command, create.telegram_id, create.username) == ("create", 123, "@boss")
    revoke = parser.parse_args(["revoke", "--telegram-id", "7", "--force"])
    assert (revoke.command, revoke.telegram_id, revoke.force) == ("revoke", 7, True)
    assert parser.parse_args(["revoke", "--telegram-id", "7"]).force is False


def test_missing_password_is_config_error(log: _Logger) -> None:
    assert cli.main(["create", "--telegram-id", "1"]) == _common.EXIT_CONFIG
    [(level, event, kw)] = log.events
    assert (level, event, kw["exit_code"]) == ("error", "superuser_failed", 2)
    assert "traceback" not in kw


def test_unexpected_error_exit_1_with_traceback(
    log: _Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run", _fake_run(error=RuntimeError("boom")))
    assert cli.main(["create", "--telegram-id", "1"]) == _common.EXIT_ERROR
    [(_, _, kw)] = log.events
    assert kw["exit_code"] == 1
    assert "traceback" in kw


def test_domain_error_exit_1_without_traceback(
    log: _Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = ValidationError("last", code=LAST_SUPERUSER)
    monkeypatch.setattr(cli, "run", _fake_run(error=error))
    assert cli.main(["revoke", "--telegram-id", "1"]) == _common.EXIT_ERROR
    [(_, _, kw)] = log.events
    assert kw["error_code"] == LAST_SUPERUSER
    assert "traceback" not in kw


def test_success_exit_0(log: _Logger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run", _fake_run(result=_result()))
    assert cli.main(["create", "--telegram-id", "42"]) == _common.EXIT_OK
    assert [(level, event) for level, event, _ in log.events] == [("info", "superuser_done")]


def test_cache_failure_is_warning_exit_0(log: _Logger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run", _fake_run(result=_result(cache_invalidated=False)))
    assert cli.main(["create", "--telegram-id", "42"]) == _common.EXIT_OK
    assert [level for level, _, _ in log.events] == ["warning", "info"]


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
        (ConfigurationError("bad"), 2),
        (_validation_error(), 2),
        (ValueError("x"), 1),
        (ValidationError("x", code="x"), 1),
    ],
)
def test_common_exit_code_for(error: BaseException, code: int) -> None:
    assert _common.exit_code_for(error) == code


def test_common_exit_code_for_extra_config_errors() -> None:
    assert _common.exit_code_for(KeyError("x"), config_errors=(KeyError,)) == 2
    assert _common.first_line(RuntimeError("a\nb")) == "a"
