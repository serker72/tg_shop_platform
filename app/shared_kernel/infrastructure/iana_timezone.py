"""Bind-time validation for timezone IDs stored as plain SQL TEXT."""

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.shared_kernel.timezones import validate_timezone


class IanaTimezone(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else validate_timezone(value)
