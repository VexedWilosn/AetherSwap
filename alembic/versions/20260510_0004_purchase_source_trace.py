from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0004"
down_revision = "20260510_0003"
branch_labels = None
depends_on = None


TABLE_NAME = "purchase"


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
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(), nullable=False, server_default=""),
            sa.Column("goods_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("at", sa.Float(), nullable=False, server_default="0"),
            sa.Column("market_price", sa.Float(), nullable=True),
            sa.Column("sale_price", sa.Float(), nullable=True),
            sa.Column("sold_at", sa.Float(), nullable=True),
            sa.Column("pending_receipt", sa.Boolean(), nullable=True),
            sa.Column("assetid", sa.String(), nullable=True),
            sa.Column("listing", sa.Boolean(), nullable=True),
            sa.Column("listing_status", sa.String(), nullable=True),
            sa.Column("source_platform", sa.String(length=50), nullable=True),
            sa.Column("source_action_id", sa.Integer(), nullable=True),
            sa.Column("source_order_id", sa.String(length=128), nullable=True),
            sa.Column("source_trade_offer_id", sa.String(length=128), nullable=True),
            sa.Column("source_fill_index", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_purchase_source_action_fill",
            TABLE_NAME,
            ["source_action_id", "source_fill_index"],
            unique=True,
        )
        op.create_index("ix_purchase_source_order", TABLE_NAME, ["source_platform", "source_order_id"])
        op.create_index("ix_purchase_source_trade_offer_id", TABLE_NAME, ["source_trade_offer_id"])
        return
    existing = _existing_columns()
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "source_platform" not in existing:
            batch.add_column(sa.Column("source_platform", sa.String(length=50), nullable=True))
        if "source_action_id" not in existing:
            batch.add_column(sa.Column("source_action_id", sa.Integer(), nullable=True))
        if "source_order_id" not in existing:
            batch.add_column(sa.Column("source_order_id", sa.String(length=128), nullable=True))
        if "source_trade_offer_id" not in existing:
            batch.add_column(sa.Column("source_trade_offer_id", sa.String(length=128), nullable=True))
        if "source_fill_index" not in existing:
            batch.add_column(sa.Column("source_fill_index", sa.Integer(), nullable=True))

    indexes = _existing_indexes()
    if "ix_purchase_source_action_fill" not in indexes:
        op.create_index(
            "ix_purchase_source_action_fill",
            TABLE_NAME,
            ["source_action_id", "source_fill_index"],
            unique=True,
        )
    if "ix_purchase_source_order" not in indexes:
        op.create_index("ix_purchase_source_order", TABLE_NAME, ["source_platform", "source_order_id"])
    if "ix_purchase_source_trade_offer_id" not in indexes:
        op.create_index("ix_purchase_source_trade_offer_id", TABLE_NAME, ["source_trade_offer_id"])


def downgrade() -> None:
    if not _table_exists():
        return
    indexes = _existing_indexes()
    for index_name in (
        "ix_purchase_source_trade_offer_id",
        "ix_purchase_source_order",
        "ix_purchase_source_action_fill",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name=TABLE_NAME)
    existing = _existing_columns()
    with op.batch_alter_table(TABLE_NAME) as batch:
        for column_name in (
            "source_fill_index",
            "source_trade_offer_id",
            "source_order_id",
            "source_action_id",
            "source_platform",
        ):
            if column_name in existing:
                batch.drop_column(column_name)
