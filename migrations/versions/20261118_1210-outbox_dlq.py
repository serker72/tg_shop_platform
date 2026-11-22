"""faststream-outbox dead-letter archive.

Revision ID: 20261118_1210
Revises: 20261118_1200
"""

from collections.abc import Sequence

from alembic import op
from faststream_outbox import make_dlq_table
from sqlalchemy import MetaData

revision: str = "20261118_1210"
down_revision: str | None = "20261118_1200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata = MetaData()
    make_dlq_table(metadata, table_name="outbox_dlq").create(op.get_bind())


def downgrade() -> None:
    op.drop_table("outbox_dlq")
