from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260512_0005"
down_revision = "20260510_0004"
branch_labels = None
depends_on = None


TABLE_NAME = "platform_action"


def _table_exists() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()


def _existing_columns() -> set[str]:
    if not _table_exists():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def _existing_indexes() -> set[str]:
    if not _table_exists():
        return set()
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)}


def upgrade() -> None:
    if not _table_exists():
        return
    existing = _existing_columns()
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "archived_at" not in existing:
            batch.add_column(sa.Column("archived_at", sa.Float(), nullable=True))
        if "archived_reason" not in existing:
            batch.add_column(sa.Column("archived_reason", sa.Text(), nullable=True))
        if "archived_by" not in existing:
            batch.add_column(sa.Column("archived_by", sa.String(length=80), nullable=True))
    if "ix_platform_action_archived_at" not in _existing_indexes():
        op.create_index("ix_platform_action_archived_at", TABLE_NAME, ["archived_at"])


def downgrade() -> None:
    if not _table_exists():
        return
    if "ix_platform_action_archived_at" in _existing_indexes():
        op.drop_index("ix_platform_action_archived_at", table_name=TABLE_NAME)
    existing = _existing_columns()
    with op.batch_alter_table(TABLE_NAME) as batch:
        for column_name in ("archived_by", "archived_reason", "archived_at"):
            if column_name in existing:
                batch.drop_column(column_name)
