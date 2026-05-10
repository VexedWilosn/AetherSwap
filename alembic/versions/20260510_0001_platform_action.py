from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0001"
down_revision = None
branch_labels = None
depends_on = None


TABLE_NAME = "platform_action"


def _existing_indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(TABLE_NAME)}
    except sa.exc.NoSuchTableError:
        return set()


def _create_index_if_missing(name: str, columns: list[str], *, unique: bool = False) -> None:
    if name not in _existing_indexes():
        op.create_index(name, TABLE_NAME, columns, unique=unique)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("finished_at", sa.Float(), nullable=True),
            sa.Column("next_check_at", sa.Float(), nullable=False),
            sa.Column("lease_until", sa.Float(), nullable=True),
            sa.Column("action_type", sa.String(length=64), nullable=False),
            sa.Column("platform", sa.String(length=50), nullable=False),
            sa.Column("state", sa.String(length=40), nullable=False),
            sa.Column("channel", sa.String(length=40), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("market_hash_name", sa.String(length=255), nullable=False),
            sa.Column("risk_category", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("target_price", sa.Float(), nullable=True),
            sa.Column("reference_price", sa.Float(), nullable=True),
            sa.Column("cost_basis_cny", sa.Float(), nullable=True),
            sa.Column("expected_profit_rate", sa.Float(), nullable=True),
            sa.Column("locked_budget_cny", sa.Float(), nullable=False),
            sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("remaining_quantity", sa.Integer(), nullable=True),
            sa.Column("filled_amount_cny", sa.Float(), nullable=False, server_default="0"),
            sa.Column("released_budget_cny", sa.Float(), nullable=False, server_default="0"),
            sa.Column("platform_order_id", sa.String(length=128), nullable=True),
            sa.Column("platform_listing_id", sa.String(length=128), nullable=True),
            sa.Column("trade_offer_id", sa.String(length=128), nullable=True),
            sa.Column("assetid", sa.String(length=128), nullable=True),
            sa.Column("idempotency_key", sa.String(length=255), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False),
            sa.Column("max_retries", sa.Integer(), nullable=False),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("request_payload", sa.Text(), nullable=True),
            sa.Column("response_payload", sa.Text(), nullable=True),
            sa.Column("raw_context", sa.Text(), nullable=True),
        )

    _create_index_if_missing("ix_platform_action_state_next_check_at", ["state", "next_check_at"])
    _create_index_if_missing(
        "ix_platform_action_platform_state_next_check_at",
        ["platform", "state", "next_check_at"],
    )
    _create_index_if_missing("ix_platform_action_item_state", ["item_id", "state"])
    _create_index_if_missing("ix_platform_action_risk_category_state", ["risk_category", "state"])
    _create_index_if_missing("ix_platform_action_platform_order_id", ["platform", "platform_order_id"])
    _create_index_if_missing("ix_platform_action_trade_offer_id", ["trade_offer_id"])
    _create_index_if_missing("ix_platform_action_assetid", ["assetid"])
    _create_index_if_missing("ix_platform_action_idempotency_key", ["idempotency_key"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE_NAME not in inspector.get_table_names():
        return
    for name in (
        "ix_platform_action_idempotency_key",
        "ix_platform_action_assetid",
        "ix_platform_action_trade_offer_id",
        "ix_platform_action_platform_order_id",
        "ix_platform_action_risk_category_state",
        "ix_platform_action_item_state",
        "ix_platform_action_platform_state_next_check_at",
        "ix_platform_action_state_next_check_at",
    ):
        if name in _existing_indexes():
            op.drop_index(name, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
