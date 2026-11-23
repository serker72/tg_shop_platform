"""Timezone preferences, independent of event timestamps.

Revision ID: 20261118_1220
Revises: 20261118_1210
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20261118_1220"
down_revision: str | None = "20261118_1210"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    # The server default initializes existing tenant rows and future inserts. This
    # literal must not depend on deployment settings or the database session timezone.
    op.add_column("tenants", sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"))
    op.add_column("users", sa.Column("timezone", sa.Text(), nullable=True))
    op.add_column("customers", sa.Column("timezone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "timezone")
    op.drop_column("users", "timezone")
    op.drop_column("tenants", "timezone")
