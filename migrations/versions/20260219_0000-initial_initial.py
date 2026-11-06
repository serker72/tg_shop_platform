"""initial

Baseline of the migration history: an empty revision with no parent.

It exists so that ``alembic upgrade head`` / ``alembic downgrade base`` are valid on a
clean database and so that every later revision has a stable starting point.

Revision ID: initial
Revises:
Create Date: 2026-02-19 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the revision.

    Intentionally empty: the first real schema (partitioned ``user_activity`` /
    ``audit_logs``, IAM tables, ...) lands in the following tasks.
    """


def downgrade() -> None:
    """Revert the revision.

    Intentionally empty, mirroring :func:`upgrade`.
    """
