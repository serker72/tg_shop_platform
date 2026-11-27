"""Persist the desired polling restart generation.

Revision ID: 20261127_1200
Revises: 20261118_1220
"""

import sqlalchemy as sa
from alembic import op

revision: str = "20261127_1200"
down_revision: str = "20261118_1220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("restart_revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("bots", "restart_revision")
