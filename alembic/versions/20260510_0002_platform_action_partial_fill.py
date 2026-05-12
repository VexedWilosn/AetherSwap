from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0002"
down_revision = "20260510_0001"
branch_labels = None
depends_on = None


TABLE_NAME = "platform_action"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    existing = _existing_columns()
    if not existing:
        return
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "filled_quantity" not in existing:
            batch.add_column(sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"))
        if "remaining_quantity" not in existing:
            batch.add_column(sa.Column("remaining_quantity", sa.Integer(), nullable=True))
        if "filled_amount_cny" not in existing:
            batch.add_column(sa.Column("filled_amount_cny", sa.Float(), nullable=False, server_default="0"))
        if "released_budget_cny" not in existing:
            batch.add_column(sa.Column("released_budget_cny", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    existing = _existing_columns()
    if not existing:
        return
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "released_budget_cny" in existing:
            batch.drop_column("released_budget_cny")
        if "filled_amount_cny" in existing:
            batch.drop_column("filled_amount_cny")
        if "remaining_quantity" in existing:
            batch.drop_column("remaining_quantity")
        if "filled_quantity" in existing:
            batch.drop_column("filled_quantity")
