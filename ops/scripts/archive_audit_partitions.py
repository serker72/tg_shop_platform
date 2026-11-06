"""Archive detached ``archive.audit_logs_pYYYYMMDD`` partitions to object storage, then drop.

Usage (project root locally, ``/app`` in the container)::

    python ops/scripts/archive_audit_partitions.py [--dry-run] [--limit N] [--tmp-dir DIR]

Exit codes: 0 - success / no-op, 1 - transient error (storage/DB unavailable),
2 - integrity or configuration error (checksum/manifest mismatch, name collision, too large).
The logic lives in :mod:`app.modules.logging.audit_archive`. ``python <file>`` puts only this
directory on ``sys.path``, so the project root is prepended to make ``import app`` work.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.logging.audit_archive import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
