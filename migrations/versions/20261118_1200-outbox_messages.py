"""faststream-outbox transport table.

Revision ID: 20261118_1200
Revises: staff_permissions
"""

from typing import Sequence, Union

from alembic import op
from faststream_outbox import make_outbox_table
from sqlalchemy import MetaData

revision: str = "20261118_1200"
down_revision: Union[str, None] = "staff_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    metadata = MetaData()
    table = make_outbox_table(metadata, table_name="outbox_messages")
    table.create(op.get_bind())


def downgrade() -> None:
    op.drop_table("outbox_messages")
