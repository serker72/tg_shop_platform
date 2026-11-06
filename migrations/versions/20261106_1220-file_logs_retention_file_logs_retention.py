"""file_logs_retention

``system_settings.file_logs_retention_days`` - retention of text click-logs (TZ 47.9),
default 90, CHECK 1..3650. Literals are frozen (no app imports).

Revision ID: file_logs_retention
Revises: partition_logs
Create Date: 2026-11-06 12:20:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "file_logs_retention"
down_revision: str | None = "partition_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column with a server default and its range CHECK."""
    op.add_column(
        "system_settings",
        sa.Column(
            "file_logs_retention_days", sa.Integer(), server_default=sa.text("90"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_system_settings_file_logs_retention_range"),
        "system_settings",
        "file_logs_retention_days BETWEEN 1 AND 3650",
    )


def downgrade() -> None:
    """Drop the CHECK and the column."""
    op.drop_constraint(
        op.f("ck_system_settings_file_logs_retention_range"), "system_settings", type_="check"
    )
    op.drop_column("system_settings", "file_logs_retention_days")
