"""Unit tests for :class:`app.shared_kernel.infrastructure.encrypted_string.EncryptedString`.

The tests use an in-memory SQLite database, so they exercise the real
``process_bind_param`` / ``process_result_value`` round trip through SQLAlchemy
without any infrastructure. Fernet keys are generated on the fly - no key
material is ever committed to the repository.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Text, create_engine, literal, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.config import Settings, get_settings
from app.shared_kernel.domain.exceptions import ConfigurationError, DomainError
from app.shared_kernel.infrastructure.encrypted_string import EncryptedString

PLAINTEXT = "123456789:AAH-secret-bot-token"

DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]

_TABLE_NAME = "unit_encrypted_string_probe"


class _TestBase(DeclarativeBase):
    """Local declarative base so the test model never pollutes the shared metadata."""


class _EncryptedModel(_TestBase):
    """Local test-only model: never imported by application code."""

    __tablename__ = _TABLE_NAME

    id: Mapped[int] = mapped_column(primary_key=True)
    secret: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)


def _bind(model_type: EncryptedString) -> str | None:
    """Encrypt through the public bind hook."""
    return model_type.process_bind_param(PLAINTEXT, DIALECT)


def _result(model_type: EncryptedString, value: str | None) -> str | None:
    """Decrypt through the public result hook."""
    return model_type.process_result_value(value, DIALECT)


def _raw_stored(session: Session) -> str:
    """Read the stored bytes bypassing the ORM and the ``TypeDecorator``."""
    table = _EncryptedModel.__table__
    statement = select(table.c.secret).select_from(table).compile(dialect=DIALECT)
    with session.connection() as connection:
        rows: Any = connection.exec_driver_sql(str(statement))
        return str(rows.scalar_one())


@pytest.fixture()
def keys() -> list[str]:
    """Three independent Fernet keys."""
    return [Fernet.generate_key().decode() for _ in range(3)]


@pytest.fixture()
def session() -> Iterator[Session]:
    """Session bound to a throw-away in-memory SQLite database."""
    engine = create_engine("sqlite://")
    _TestBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture(autouse=True)
def isolated_settings() -> Iterator[None]:
    """Clear the settings cache so the lazy lookup never leaks between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_round_trip_encrypts_and_decrypts(session: Session, keys: list[str]) -> None:
    """The value stored in the DB differs from the plaintext and decrypts back."""
    model_type = EncryptedString(keys=[keys[0]])

    ciphertext = _bind(model_type)
    assert ciphertext is not None
    assert ciphertext != PLAINTEXT
    assert PLAINTEXT not in ciphertext
    assert _result(model_type, ciphertext) == PLAINTEXT

    session.add(_EncryptedModel(secret=PLAINTEXT))
    session.commit()
    session.expunge_all()

    assert session.scalars(select(_EncryptedModel)).one().secret == PLAINTEXT

    stored = _raw_stored(session)
    assert stored != PLAINTEXT
    assert PLAINTEXT not in stored


def test_impl_is_text_not_varchar() -> None:
    """The DDL type is TEXT so the ~2x longer ciphertext is never truncated."""
    model_type = EncryptedString(keys=[Fernet.generate_key().decode()])

    assert isinstance(model_type.impl, Text)
    compiled = model_type.compile(dialect=DIALECT)
    assert compiled == "TEXT"
    assert not compiled.startswith("VARCHAR")
    # ``length`` is accepted for DDL compatibility but must not narrow the type.
    sized = EncryptedString(keys=[Fernet.generate_key().decode()], length=64)
    assert sized.compile(dialect=DIALECT) == "TEXT"


def test_key_rotation_reads_old_and_writes_new(keys: list[str]) -> None:
    """A value encrypted by the old key is readable once the new key becomes primary."""
    old_type = EncryptedString(keys=[keys[0]])
    old_ciphertext = _bind(old_type)
    assert old_ciphertext is not None

    rotated = EncryptedString(keys=[keys[1], keys[0]])
    assert _result(rotated, old_ciphertext) == PLAINTEXT

    new_ciphertext = _bind(rotated)
    assert new_ciphertext is not None
    assert new_ciphertext != old_ciphertext
    # New writes are readable with the new key alone...
    assert _result(EncryptedString(keys=[keys[1]]), new_ciphertext) == PLAINTEXT
    # ...and the retired key can no longer read them.
    with pytest.raises(ConfigurationError):
        _result(old_type, new_ciphertext)


def test_none_stays_none(session: Session, keys: list[str]) -> None:
    """``NULL`` is preserved in both directions."""
    model_type = EncryptedString(keys=[keys[0]])
    assert model_type.process_bind_param(None, DIALECT) is None
    assert model_type.process_result_value(None, DIALECT) is None

    session.add(_EncryptedModel(secret=None))
    session.commit()
    session.expunge_all()
    assert session.scalars(select(_EncryptedModel)).one().secret is None


def test_invalid_key_raises_configuration_error() -> None:
    """A malformed Fernet key is a configuration problem, reported without the key."""
    model_type = EncryptedString(keys=["not-a-fernet-key"])
    with pytest.raises(ConfigurationError) as excinfo:
        _bind(model_type)
    assert "not-a-fernet-key" not in str(excinfo.value)
    assert excinfo.value.code == "encryption_key_invalid"


def test_missing_keys_raise_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Without a configured key the lazy settings lookup fails loudly."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    # Blank env variables win over the repository ``.env`` file, and an empty
    # dotenv path keeps the project file out of the lookup entirely.
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    monkeypatch.setenv("ENCRYPTION_KEYS_OLD", "")
    monkeypatch.setattr(
        "app.shared_kernel.infrastructure.encrypted_string.get_settings",
        lambda: Settings(_env_file=str(empty_env)),
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _bind(EncryptedString())
    assert excinfo.value.code == "encryption_key_missing"


def _cache_key(model_type: EncryptedString) -> Any:  # noqa: ANN401 - opaque cache key tuple
    cache_key = literal("x", model_type)._generate_cache_key()
    assert cache_key is not None
    return cache_key.key


def test_cache_key_distinguishes_key_sets(keys: list[str]) -> None:
    """Different key lists never share a compiled-statement cache entry."""
    first = _cache_key(EncryptedString(keys=[keys[0]]))
    second = _cache_key(EncryptedString(keys=[keys[1]]))
    same = _cache_key(EncryptedString(keys=[keys[0]]))

    assert first != second
    assert first == same
    assert _cache_key(EncryptedString()) == _cache_key(EncryptedString())
    assert _cache_key(EncryptedString()) != first


def test_key_material_is_not_exposed(keys: list[str]) -> None:
    """Neither ``repr`` (used by Alembic autogenerate) nor the cache key leak keys."""
    model_type = EncryptedString(keys=[keys[0], keys[1]])
    exposed = repr(model_type) + repr(_cache_key(model_type)) + repr(model_type.copy())
    for key in keys[:2]:
        assert key not in exposed


def test_corrupted_ciphertext_raises_domain_error(keys: list[str]) -> None:
    """A tampered ciphertext raises a domain error instead of returning garbage."""
    model_type = EncryptedString(keys=[keys[0]])
    ciphertext = _bind(model_type)
    assert ciphertext is not None
    corrupted = ciphertext[:-4] + "AAAA"

    with pytest.raises(DomainError):
        _result(model_type, corrupted)

    with pytest.raises(DomainError):
        _result(model_type, "totally-not-ciphertext")
