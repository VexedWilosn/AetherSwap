import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_creates_platform_action_table(tmp_path):
    os.environ["AETHERSWAP_SKIP_IMPORT_SCHEMA_PATCHES"] = "1"
    db_path = tmp_path / "alembic.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "platform_action" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("platform_action")}
    assert {
        "action_type",
        "platform",
        "state",
        "next_check_at",
        "idempotency_key",
        "risk_category",
        "locked_budget_cny",
        "filled_quantity",
        "remaining_quantity",
        "filled_amount_cny",
        "released_budget_cny",
    }.issubset(columns)
    indexes = {idx["name"] for idx in inspector.get_indexes("platform_action")}
    assert "ix_platform_action_state_next_check_at" in indexes
    assert "ix_platform_action_idempotency_key" in indexes
    assert "ix_platform_action_risk_category_state" in indexes
