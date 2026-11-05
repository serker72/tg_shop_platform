"""Unit tests for :mod:`app.core.logging`."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from app.core.logging import (
    _HANDLER_MARKER,
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
    get_trace_id,
    new_trace_id,
)


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate global structlog/stdlib config and the trace id contextvar."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    structlog.reset_defaults()
    clear_trace_id()
    yield
    structlog.reset_defaults()
    clear_trace_id()


def read_json_lines(captured_out: str) -> list[dict[str, Any]]:
    """Parse captured stdout into JSON records."""
    return [json.loads(line) for line in captured_out.splitlines() if line.strip()]


def test_json_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)
    logger = get_logger("test")

    logger.info("hello world", extra_field=42)

    lines = read_json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert lines[0]["event"] == "hello world"
    assert lines[0]["level"] == "info"
    assert lines[0]["extra_field"] == 42


def test_trace_id_in_log(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)
    bind_trace_id("abc123")

    get_logger("test").info("with trace")

    payload = read_json_lines(capsys.readouterr().out)[0]
    assert payload["trace_id"] == "abc123"


def test_stdlib_logger_goes_to_stdout_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)

    logging.getLogger("aiogram").warning("stdlib hello")

    payload = read_json_lines(capsys.readouterr().out)[0]
    assert payload["event"] == "stdlib hello"
    assert payload["level"] == "warning"
    assert payload["logger"] == "aiogram"


def test_exception_produces_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("test").exception("failed")

    payload = read_json_lines(capsys.readouterr().out)[0]
    assert payload["event"] == "failed"
    exception = payload["exception"]
    assert exception
    assert "RuntimeError" in json.dumps(exception)


def test_stdlib_exception_produces_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("faststream").exception("failed")

    payload = read_json_lines(capsys.readouterr().out)[0]
    assert payload["exception"]


@pytest.mark.parametrize("level", ["WARN", "warning", "info", "DEBUG", 30])
def test_level_normalisation(capsys: pytest.CaptureFixture[str], level: str | int) -> None:
    configure_logging(level=level, json_logs=True)

    get_logger("test").error("error line")

    assert read_json_lines(capsys.readouterr().out)[0]["level"] == "error"


def test_unknown_level_raises() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging(level="NOPE")


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json_logs=True)
    logger = get_logger("test")

    logger.info("hidden")
    logger.warning("shown")

    payloads = read_json_lines(capsys.readouterr().out)
    assert [payload["event"] for payload in payloads] == ["shown"]


def test_console_output_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=False)

    get_logger("test").info("plain text", answer=42)

    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())
    assert "plain text" in out
    assert "answer" in out


def test_new_trace_id_generates_and_binds() -> None:
    generated = new_trace_id()

    assert generated
    assert get_trace_id() == generated


def test_new_trace_id_uses_given_value() -> None:
    assert new_trace_id("fixed-id") == "fixed-id"
    assert get_trace_id() == "fixed-id"


def test_clear_trace_id_without_token() -> None:
    bind_trace_id("abc123")

    clear_trace_id()

    assert get_trace_id() is None


def test_clear_trace_id_restores_previous_state() -> None:
    outer = bind_trace_id("outer")

    assert get_trace_id() == "outer"

    clear_trace_id(outer)

    assert get_trace_id() is None


async def test_trace_id_isolated_between_tasks() -> None:
    configure_logging(json_logs=True)
    seen: dict[str, str | None] = {}

    async def worker(name: str) -> None:
        new_trace_id(name)
        await asyncio.sleep(0)
        seen[name] = get_trace_id()

    await asyncio.gather(worker("first"), worker("second"))

    assert seen == {"first": "first", "second": "second"}
    assert get_trace_id() is None


def _own_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_MARKER, False)]


def test_repeated_configure_does_not_duplicate_handlers() -> None:
    configure_logging(json_logs=True)
    configure_logging(json_logs=True)

    assert len(_own_handlers()) == 1


def test_repeated_configure_keeps_foreign_handlers() -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        configure_logging(json_logs=True)
        configure_logging(json_logs=True)

        assert foreign in root.handlers
        assert len(_own_handlers()) == 1
    finally:
        root.removeHandler(foreign)


def test_stdlib_handlers_use_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True)

    logging.getLogger("aiogram").info("captured")

    captured = capsys.readouterr()
    assert "captured" in captured.out
    assert captured.err == ""


def test_level_from_string_binds_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=True, level="DEBUG")

    get_logger("x").debug("debug message")

    assert read_json_lines(capsys.readouterr().out)[0]["level"] == "debug"
