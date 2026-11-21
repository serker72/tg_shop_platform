"""Create/update the read-only PostgreSQL monitoring role (member of ``pg_monitor``).

Usage (project root locally, ``/app`` in the container; called by ``ops/scripts/migrate.sh``)::

    python ops/scripts/ensure_monitoring_role.py

Env: ``POSTGRES_MONITORING_USER`` (default ``monitoring``), ``POSTGRES_MONITORING_PASSWORD``
(empty = step skipped with a warning; placeholders like ``change_me`` are rejected),
connection as ``POSTGRES_*`` (owner/superuser of the cluster).
Idempotent: CREATE ROLE on first run, ALTER ROLE (password sync) on every next run.
Also grants read access to ``partman.part_config`` and the maintenance flag column of
``public.system_settings`` (custom Alloy metrics).
The password never reaches the server or the logs in plain text: a SCRAM-SHA-256 verifier
is computed locally and sent instead (PostgreSQL stores verifiers as is).
The role is cluster-wide, so it is managed here and not in an Alembic migration
(migrations are per-database and must not depend on runtime secrets).
Exit codes: 0 - success / skipped, 2 - configuration error, 1 - database error.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from app.core.config import get_settings  # noqa: E402

_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SCRAM_ITERATIONS = 4096
# values copied from templates as is: never accepted as a real password
_PLACEHOLDER_PASSWORDS = frozenset({"change_me", "changeme", "password", "monitoring"})


def _log(message: str) -> None:
    print(f"[migrate] {message}", file=sys.stderr, flush=True)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def scram_sha256_verifier(password: str, iterations: int = _SCRAM_ITERATIONS) -> str:
    """Return a PostgreSQL SCRAM-SHA-256 verifier (RFC 5802/7677, ASCII passwords)."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"


def main() -> int:
    role = os.environ.get("POSTGRES_MONITORING_USER", "").strip() or "monitoring"
    password = os.environ.get("POSTGRES_MONITORING_PASSWORD", "")
    if not password.strip():
        _log(
            "WARNING: POSTGRES_MONITORING_PASSWORD is empty: monitoring role skipped "
            "(required for the Alloy postgres exporter; generate: openssl rand -hex 24)"
        )
        return 0
    if password.strip().lower() in _PLACEHOLDER_PASSWORDS:
        _log(
            "POSTGRES_MONITORING_PASSWORD is a placeholder value: "
            "set a real secret (openssl rand -hex 24)"
        )
        return 2
    if not _ROLE_RE.match(role):
        _log(f"POSTGRES_MONITORING_USER={role!r} is not a valid lower-case role name")
        return 2
    pg = get_settings().postgres
    if pg.password is None:
        _log("POSTGRES_PASSWORD is not configured")
        return 2
    if role == pg.user:
        _log("POSTGRES_MONITORING_USER must differ from POSTGRES_USER")
        return 2

    ident = sql.Identifier(role)
    verifier = sql.Literal(scram_sha256_verifier(password))
    options = sql.SQL(
        "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5"
    )
    try:
        with psycopg.connect(
            host=pg.host,
            port=pg.port,
            dbname=pg.db,
            user=pg.user,
            password=pg.password.get_secret_value(),
            connect_timeout=10,
            autocommit=True,
        ) as conn:
            exists = (
                conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                is not None
            )
            if exists:
                stmt = sql.SQL("ALTER ROLE {} WITH {} PASSWORD {}")
            else:
                stmt = sql.SQL("CREATE ROLE {} WITH {} PASSWORD {}")
            conn.execute(stmt.format(ident, options, verifier))
            conn.execute(sql.SQL("GRANT pg_monitor TO {}").format(ident))
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(pg.db), ident)
            )
            conn.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(ident)
            )
            # pg_partman health metrics (ops/observability/alloy/postgres_queries.yml);
            # the schema appears with the first migration, GRANT is idempotent
            if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'partman'").fetchone():
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA partman TO {}").format(ident))
                conn.execute(sql.SQL("GRANT SELECT ON partman.part_config TO {}").format(ident))
            else:
                _log("schema partman not found: pg_partman metrics grants skipped")
            settings_exists = conn.execute(
                "SELECT to_regclass('public.system_settings') IS NOT NULL"
            ).fetchone()
            if settings_exists == (True,):
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
                conn.execute(
                    sql.SQL(
                        "GRANT SELECT (is_maintenance_mode) ON public.system_settings TO {}"
                    ).format(ident)
                )
            else:
                _log("table public.system_settings not found: maintenance metric grant skipped")
    except psycopg.Error as error:
        # no statement text / DSN in the message: only the error class and SQLSTATE
        _log(f"monitoring role: {type(error).__name__} (SQLSTATE {error.sqlstate})")
        return 1
    _log(f"monitoring role {role!r} {'updated' if exists else 'created'} (pg_monitor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
