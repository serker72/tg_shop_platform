"""Shared helpers of the ``app.entrypoints.cli`` commands: exit codes and failure logging.

Exit codes: 0 - success, 1 - runtime error, 2 - configuration/argument error.
"""

from __future__ import annotations

import traceback
from typing import Any, Final

import pydantic
from sqlalchemy.exc import OperationalError

from app.shared_kernel.domain.exceptions import ConfigurationError

__all__ = ["EXIT_CONFIG", "EXIT_ERROR", "EXIT_OK", "exit_code_for", "first_line", "log_failure"]

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_CONFIG: Final = 2


def exit_code_for(
    error: BaseException, *, config_errors: tuple[type[BaseException], ...] = ()
) -> int:
    """``2`` for configuration problems (:class:`ConfigurationError`, invalid settings,
    ``config_errors``), ``1`` for anything else.
    """
    if isinstance(error, (ConfigurationError, pydantic.ValidationError, *config_errors)):
        return EXIT_CONFIG
    return EXIT_ERROR


def first_line(error: BaseException) -> str:
    """First line of ``str(error)`` (multi-line driver errors stay readable in one record)."""
    text = str(error).strip()
    return text.splitlines()[0] if text else ""


def log_failure(
    logger: Any,
    event: str,
    error: BaseException,
    *,
    quiet: tuple[type[BaseException], ...] = (OperationalError,),
    **extra: Any,
) -> int:
    """Log ``error`` as ``event`` and return its exit code.

    Never ``exc_info=True``: the shared ``dict_tracebacks`` processor renders frame locals,
    and connection frames hold the password. Unexpected errors (exit code 1, not ``quiet``)
    get a ``traceback`` from ``format_exception``, which has no locals.
    """
    code = exit_code_for(error)
    details: dict[str, Any] = dict(extra)
    if code == EXIT_ERROR and not isinstance(error, quiet):
        details["traceback"] = "".join(traceback.format_exception(error))
    logger.error(
        event,
        error_type=type(error).__name__,
        error=first_line(error),
        exit_code=code,
        **details,
    )
    return code
