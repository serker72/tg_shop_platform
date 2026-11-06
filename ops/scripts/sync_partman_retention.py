"""Sync ``partman.part_config.retention`` with ``public.system_settings``.

Usage (project root locally, ``/app`` in the container)::

    python ops/scripts/sync_partman_retention.py [--dry-run]

Exit codes: 0 - success / no-op, 2 - guard or configuration error, 1 - other errors.
The logic lives in :mod:`app.modules.logging.retention`. ``python <file>`` puts only this
directory on ``sys.path``, so the project root is prepended to make ``import app`` work
regardless of the current directory or ``PYTHONPATH``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.logging.retention import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
