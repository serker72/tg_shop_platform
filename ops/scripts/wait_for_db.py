"""Wait until PostgreSQL accepts connections (``POSTGRES_*`` settings).

Used by ``ops/scripts/migrate.sh`` instead of ``pg_isready``: the application image
(``python:*-slim``) has no PostgreSQL client. Timeout: ``MIGRATE_WAIT_SECONDS`` (default 60),
one attempt per second. Exit 0 when connected, 1 on timeout/misconfiguration.
Every ``OperationalError`` is treated as "not ready yet": a wrong password, a missing
database or a closed port all keep the loop going until the timeout, then exit code 1
(with the first line of the last error). No DSN or password is printed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.core.config import get_settings  # noqa: E402


def _log(message: str) -> None:
    print(f"[migrate] {message}", file=sys.stderr, flush=True)


def main() -> int:
    raw = os.environ.get("MIGRATE_WAIT_SECONDS", "60")
    try:
        timeout = int(raw)
    except ValueError:
        _log(f"MIGRATE_WAIT_SECONDS must be an integer, got {raw!r}")
        return 1
    pg = get_settings().postgres
    if pg.password is None:
        _log("POSTGRES_PASSWORD is not configured")
        return 1
    where = f"{pg.host}:{pg.port}/{pg.db}"
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        remaining = deadline - time.monotonic()
        try:
            with psycopg.connect(
                host=pg.host,
                port=pg.port,
                dbname=pg.db,
                user=pg.user,
                password=pg.password.get_secret_value(),
                connect_timeout=max(1, min(5, int(remaining))),
            ) as conn:
                conn.execute("SELECT 1")
        except psycopg.OperationalError as error:
            if time.monotonic() >= deadline:
                detail = str(error).strip().splitlines()[0] if str(error).strip() else ""
                _log(f"PostgreSQL at {where} is not ready after {timeout}s: {detail}")
                return 1
            time.sleep(1)
            continue
        _log(f"PostgreSQL at {where} is ready")
        return 0


if __name__ == "__main__":
    sys.exit(main())
