"""Structured logging setup based on structlog.

structlog and the stdlib ``logging`` module share one pipeline and write to ``stdout``
(container convention; collected by Grafana Alloy later). ``configure_logging`` is
idempotent: repeated calls never accumulate handlers.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from typing import Any, cast

import structlog

__all__ = [
    "bind_trace_id",
    "clear_trace_id",
    "configure_logging",
    "get_logger",
    "get_trace_id",
    "new_trace_id",
]

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

# Attribute marking root handlers installed by ``configure_logging``.
_HANDLER_MARKER = "_tg_shop_platform_handler"

_LEVEL_NAMES: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def _resolve_level(level: str | int) -> int:
    """Normalise a level name or number to a stdlib logging level."""
    if isinstance(level, str):
        resolved = _LEVEL_NAMES.get(level.strip().upper())
        if resolved is not None:
            return resolved
    elif isinstance(level, int) and level in _LEVEL_NAMES.values():
        return level
    allowed = ", ".join(sorted(_LEVEL_NAMES))
    raise ValueError(f"Unknown log level {level!r}; expected one of: {allowed} or an int level")


def _put_trace_id(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor adding ``trace_id`` when it is bound to the context."""
    trace_id = _trace_id_var.get()
    if trace_id:
        event_dict["trace_id"] = trace_id
    return event_dict


def bind_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    """Bind ``trace_id`` to the current context and return the reset token."""
    return _trace_id_var.set(trace_id)


def clear_trace_id(token: contextvars.Token[str | None] | None = None) -> None:
    """Reset the trace id: restore the state captured by ``token`` or drop it."""
    if token is not None:
        _trace_id_var.reset(token)
        return
    _trace_id_var.set(None)


def new_trace_id(trace_id: str | None = None) -> str:
    """Bind and return the given trace id, or a freshly generated one."""
    value = trace_id if trace_id else uuid.uuid4().hex
    _trace_id_var.set(value)
    return value


def get_trace_id() -> str | None:
    """Return the trace id bound to the current context, if any."""
    return _trace_id_var.get()


def _renderer(json_logs: bool) -> Any:
    """Return the final renderer for the chosen output format."""
    if json_logs:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


def _shared_processors() -> list[Any]:
    """Processors applied to both structlog and foreign stdlib records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _put_trace_id,
        structlog.processors.dict_tracebacks,
    ]


def configure_logging(level: str | int = "INFO", *, json_logs: bool = True) -> None:
    """Configure structlog and the stdlib root logger to log to stdout.

    Idempotent: the root logger keeps exactly one handler installed by this function after
    any number of calls. Handlers installed by other code are left untouched.

    Raises:
        ValueError: if ``level`` is not a known level name or standard level number.
    """
    resolved = _resolve_level(level)
    shared_processors = _shared_processors()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(json_logs),
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    stream_handler = logging.StreamHandler(sys.stdout)
    setattr(stream_handler, _HANDLER_MARKER, True)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.setLevel(resolved)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger with stdlib integration."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
