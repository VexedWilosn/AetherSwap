from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0003"
down_revision = "20260510_0002"
branch_labels = None
depends_on = None


TABLE_NAME = "platform_action"
INDEX_NAME = "ix_platform_action_risk_category_state"


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
    existing_columns = _existing_columns()
    if "risk_category" not in existing_columns:
        with op.batch_alter_table(TABLE_NAME) as batch:
            batch.add_column(sa.Column("risk_category", sa.String(length=255), nullable=False, server_default=""))
    if INDEX_NAME not in _existing_indexes():
        op.create_index(INDEX_NAME, TABLE_NAME, ["risk_category", "state"], unique=False)


def downgrade() -> None:
    if not _table_exists():
        return
    if INDEX_NAME in _existing_indexes():
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    if "risk_category" in _existing_columns():
        with op.batch_alter_table(TABLE_NAME) as batch:
            batch.drop_column("risk_category")
