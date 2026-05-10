from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from DataEngine.sqlite_pragmas import install_sqlite_pragmas


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式基类。"""


class ItemBase(Base):
    """【饰品主表】所有平台共享的唯一真理之源。"""

    __tablename__ = "item_base"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    cn_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buff_goods_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uuyp_template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eco_goods_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    game: Mapped[str] = mapped_column(String(20), default="csgo", nullable=False)

    crawl_priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    priority_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    priority_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    priority_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority_ttl_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority_up_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority_down_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    manual_watch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_profit_margin: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    radar_last_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    mappings: Mapped[list["PlatformMapping"]] = relationship(back_populates="item")
    prices: Mapped[list["MarketPrice"]] = relationship(back_populates="item")
    opportunities: Mapped[list["ArbitrageOpportunity"]] = relationship(back_populates="item")
    action_decisions: Mapped[list["ActionDecision"]] = relationship(back_populates="item")


class PlatformMapping(Base):
    """【平台映射表】无论以后加多少平台，都不用改表结构。"""

    __tablename__ = "platform_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), index=True, nullable=False)
    platform_name: Mapped[str] = mapped_column(String(50), nullable=False)
    platform_item_id: Mapped[str] = mapped_column(String(255), nullable=False)

    item: Mapped[ItemBase] = relationship(back_populates="mappings")

    __table_args__ = (
        UniqueConstraint("item_id", "platform_name", name="uq_platform_mapping_item_platform"),
    )


class MarketPrice(Base):
    """【市场盘口表】三层爬虫统一的价格与流动性快照。"""

    __tablename__ = "market_price"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 保留复合主键逻辑：同一饰品在同一平台只能有一条当前盘口记录
    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), nullable=False)
    platform_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 数据来源：baseline / radar / sniper
    data_source: Mapped[str] = mapped_column(String(20), nullable=False)

    # 价格字段明确化
    sell_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_top5_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_top5_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="CNY", nullable=False)

    # 流动性字段
    volume: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    sell_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buy_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orderbook_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orderbook_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_source: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 时效性字段：默认当前时间，更新时自动刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    item: Mapped[ItemBase] = relationship(back_populates="prices")

    __table_args__ = (
        UniqueConstraint("item_id", "platform_name", name="uq_market_price_item_platform"),
    )


class RadarSnapshot(Base):
    """Precomputed radar row used by the WebUI for fast filtering and sorting."""

    __tablename__ = "radar_snapshot"

    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), primary_key=True)
    item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    buff_goods_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uuyp_template_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eco_goods_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crawl_priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    priority_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    priority_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    radar_last_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    best_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    best_platform_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    best_platform_buy_max: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_buy_max: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_sell_min: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    best_profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reverse_profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    best_direction: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cash_to_steam_profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cash_to_steam_profit_cny: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cash_to_steam_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cash_to_steam_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_to_cash_profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_to_cash_profit_cny: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_to_cash_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    steam_to_cash_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    best_profit_cny: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    steam_balance_cost_ratio: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)
    steam_crossed_book: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    steam_data_source: Mapped[str | None] = mapped_column(String(30), nullable=True)

    volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    volume_24h: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sell_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buy_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orderbook_depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orderbook_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    liquidity_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    liquidity_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="CNY", nullable=False)

    platform_payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    snapshot_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_radar_snapshot_profit", "best_profit_rate"),
        Index("ix_radar_snapshot_cash_to_steam", "cash_to_steam_profit_rate"),
        Index("ix_radar_snapshot_steam_to_cash", "steam_to_cash_profit_rate"),
        Index("ix_radar_snapshot_liquidity", "liquidity_score"),
        Index("ix_radar_snapshot_depth", "depth"),
        Index("ix_radar_snapshot_priority_profit", "crawl_priority", "best_profit_rate"),
        Index("ix_radar_snapshot_market_hash", "market_hash_name"),
    )


class ArbitrageOpportunity(Base):
    """【套利雷达机会表】前端快速读取的轻量级机会索引表。"""

    __tablename__ = "arbitrage_opportunity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), index=True, nullable=False)

    buy_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    buy_price: Mapped[float] = mapped_column(Float, nullable=False)

    sell_platform: Mapped[str] = mapped_column(String(50), default="steam", nullable=False)
    sell_price: Mapped[float] = mapped_column(Float, nullable=False)

    profit_cny: Mapped[float] = mapped_column(Float, nullable=False)
    profit_rate: Mapped[float] = mapped_column(Float, nullable=False)
    action: Mapped[str] = mapped_column(String(50), default="direct_trade", nullable=False)

    # open：发现机会；verifying：尖刀核实中；closed：机会消失
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    item: Mapped[ItemBase] = relationship(back_populates="opportunities")

    __table_args__ = (
        UniqueConstraint("item_id", "buy_platform", "sell_platform", name="uq_arb_item_buy_sell"),
    )


class SteamDTOpportunity(Base):
    """SteamDT strategy-level opportunity cache used before JIT verification."""

    __tablename__ = "steamdt_opportunity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), index=True, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(80), nullable=False)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_name: Mapped[str] = mapped_column(String(50), nullable=False)

    steam_sell_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    steam_buy_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_sell_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_buy_max: Mapped[float | None] = mapped_column(Float, nullable=True)

    transaction_count_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_sell_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    platform_buy_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)

    profit_cny: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="CNY", nullable=False)
    link_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    steam_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    platform_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    steamdt_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("item_id", "strategy_name", "platform_name", name="uq_steamdt_opp_item_strategy_platform"),
    )


class ActionDecision(Base):
    """Execution plan generated by action_policy before trade_executor runs."""

    __tablename__ = "action_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("arbitrage_opportunity.id"), index=True, nullable=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item_base.id"), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    sell_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    expected_profit_cny: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    expected_profit_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    requires_jit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    risk_flags: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    item: Mapped[ItemBase] = relationship(back_populates="action_decisions")

    __table_args__ = (
        UniqueConstraint("opportunity_id", "action", "target_platform", name="uq_action_decision_opp_action_platform"),
    )


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "config" / "market_data.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_PATH.resolve()}"

# SQLite 需要关闭线程检查以兼容多线程任务；future=True 启用 SQLAlchemy 2.0 风格
engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)
install_sqlite_pragmas(engine)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def apply_schema_patches() -> None:
    """Native SQLite hot patches for columns added after the database already exists."""

    item_base_columns = {
        "buff_goods_id": "INTEGER",
        "uuyp_template_id": "INTEGER",
        "eco_goods_id": "INTEGER",
        "radar_last_matched_at": "DATETIME",
        "priority_score": "FLOAT NOT NULL DEFAULT 0",
        "priority_reason": "VARCHAR(512)",
        "priority_source": "VARCHAR(50)",
        "priority_updated_at": "DATETIME",
        "priority_ttl_until": "DATETIME",
        "priority_cooldown_until": "DATETIME",
        "priority_up_hits": "INTEGER NOT NULL DEFAULT 0",
        "priority_down_hits": "INTEGER NOT NULL DEFAULT 0",
        "manual_watch": "BOOLEAN NOT NULL DEFAULT 0",
    }
    market_price_columns = {
        "volume": "INTEGER DEFAULT 0",
        "sell_volume": "INTEGER",
        "buy_volume": "INTEGER",
        "orderbook_depth": "INTEGER",
        "orderbook_balance": "FLOAT",
        "liquidity_score": "FLOAT",
        "liquidity_source": "VARCHAR(30)",
        "currency": "VARCHAR(10) NOT NULL DEFAULT 'CNY'",
    }
    arbitrage_columns = {
        "action": "VARCHAR(50) NOT NULL DEFAULT 'direct_trade'",
    }
    steamdt_columns = {
        "steam_sell_min": "FLOAT",
        "steam_buy_max": "FLOAT",
        "platform_sell_min": "FLOAT",
        "platform_buy_max": "FLOAT",
        "transaction_count_24h": "INTEGER",
        "platform_sell_volume": "INTEGER",
        "platform_buy_volume": "INTEGER",
        "currency": "VARCHAR(10) NOT NULL DEFAULT 'CNY'",
        "link_url": "VARCHAR(1024)",
        "steam_updated_at": "DATETIME",
        "platform_updated_at": "DATETIME",
        "steamdt_updated_at": "DATETIME",
    }

    def ensure_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if not table:
            return
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for col_name, col_type in columns.items():
            if col_name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                logger.info("SQLite schema patch added | table=%s column=%s", table_name, col_name)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    logger.warning("SQLite schema patch failed | table=%s column=%s err=%s", table_name, col_name, exc)

    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
            ensure_columns(conn, "item_base", item_base_columns)
            ensure_columns(conn, "market_price", market_price_columns)
            ensure_columns(conn, "arbitrage_opportunity", arbitrage_columns)
            ensure_columns(conn, "steamdt_opportunity", steamdt_columns)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS radar_snapshot (
                    item_id INTEGER PRIMARY KEY,
                    item_name VARCHAR(255),
                    market_hash_name VARCHAR(255) NOT NULL,
                    buff_goods_id INTEGER,
                    uuyp_template_id INTEGER,
                    eco_goods_id INTEGER,
                    crawl_priority INTEGER NOT NULL DEFAULT 0,
                    priority_score FLOAT NOT NULL DEFAULT 0,
                    priority_reason VARCHAR(512),
                    priority_source VARCHAR(50),
                    radar_last_matched_at DATETIME,
                    best_platform VARCHAR(50),
                    best_platform_price FLOAT NOT NULL DEFAULT 0,
                    best_platform_buy_max FLOAT NOT NULL DEFAULT 0,
                    steam_buy_max FLOAT NOT NULL DEFAULT 0,
                    steam_sell_min FLOAT NOT NULL DEFAULT 0,
                    best_profit_rate FLOAT NOT NULL DEFAULT 0,
                    profit_rate FLOAT NOT NULL DEFAULT 0,
                    reverse_profit_rate FLOAT NOT NULL DEFAULT 0,
                    best_direction VARCHAR(40),
                    cash_to_steam_profit_rate FLOAT NOT NULL DEFAULT 0,
                    cash_to_steam_profit_cny FLOAT NOT NULL DEFAULT 0,
                    cash_to_steam_platform VARCHAR(50),
                    cash_to_steam_price FLOAT NOT NULL DEFAULT 0,
                    steam_to_cash_profit_rate FLOAT NOT NULL DEFAULT 0,
                    steam_to_cash_profit_cny FLOAT NOT NULL DEFAULT 0,
                    steam_to_cash_platform VARCHAR(50),
                    steam_to_cash_price FLOAT NOT NULL DEFAULT 0,
                    best_profit_cny FLOAT NOT NULL DEFAULT 0,
                    steam_balance_cost_ratio FLOAT NOT NULL DEFAULT 0.85,
                    steam_crossed_book BOOLEAN NOT NULL DEFAULT 0,
                    steam_data_source VARCHAR(30),
                    volume INTEGER NOT NULL DEFAULT 0,
                    volume_24h INTEGER NOT NULL DEFAULT 0,
                    depth INTEGER NOT NULL DEFAULT 0,
                    sell_volume INTEGER NOT NULL DEFAULT 0,
                    buy_volume INTEGER NOT NULL DEFAULT 0,
                    orderbook_depth INTEGER NOT NULL DEFAULT 0,
                    orderbook_balance FLOAT NOT NULL DEFAULT 0,
                    liquidity_platform VARCHAR(50),
                    liquidity_score FLOAT NOT NULL DEFAULT 0,
                    currency VARCHAR(10) NOT NULL DEFAULT 'CNY',
                    platform_payload_json TEXT NOT NULL DEFAULT '{}',
                    snapshot_updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(item_id) REFERENCES item_base(id)
                )
                """
            )
            ensure_columns(
                conn,
                "radar_snapshot",
                {
                    "cash_to_steam_profit_rate": "FLOAT NOT NULL DEFAULT 0",
                    "cash_to_steam_profit_cny": "FLOAT NOT NULL DEFAULT 0",
                    "cash_to_steam_platform": "VARCHAR(50)",
                    "cash_to_steam_price": "FLOAT NOT NULL DEFAULT 0",
                    "steam_to_cash_profit_rate": "FLOAT NOT NULL DEFAULT 0",
                    "steam_to_cash_profit_cny": "FLOAT NOT NULL DEFAULT 0",
                    "steam_to_cash_platform": "VARCHAR(50)",
                    "steam_to_cash_price": "FLOAT NOT NULL DEFAULT 0",
                    "best_profit_cny": "FLOAT NOT NULL DEFAULT 0",
                    "steam_balance_cost_ratio": "FLOAT NOT NULL DEFAULT 0.85",
                },
            )
            radar_indexes = (
                "CREATE INDEX IF NOT EXISTS ix_item_base_crawl_priority ON item_base(crawl_priority)",
                "CREATE INDEX IF NOT EXISTS ix_market_price_item_platform ON market_price(item_id, platform_name)",
                "CREATE INDEX IF NOT EXISTS ix_market_price_platform_updated ON market_price(platform_name, updated_at)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_profit ON radar_snapshot(best_profit_rate)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_cash_to_steam ON radar_snapshot(cash_to_steam_profit_rate)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_steam_to_cash ON radar_snapshot(steam_to_cash_profit_rate)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_liquidity ON radar_snapshot(liquidity_score)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_depth ON radar_snapshot(depth)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_priority_profit ON radar_snapshot(crawl_priority, best_profit_rate)",
                "CREATE INDEX IF NOT EXISTS ix_radar_snapshot_market_hash ON radar_snapshot(market_hash_name)",
            )
            for sql in radar_indexes:
                conn.execute(sql)
            conn.commit()
    except Exception as exc:
        logger.warning("SQLite schema patch exception | err=%s", exc)


def _ensure_item_base_columns() -> None:
    """为已存在的 SQLite 库补齐新增列，避免老库直接报 no such column。"""

    try:
        with engine.connect() as conn:
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(item_base)").all()}
            if "buff_goods_id" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN buff_goods_id INTEGER")
                conn.commit()
            if "uuyp_template_id" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN uuyp_template_id INTEGER")
                conn.commit()
            if "eco_goods_id" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN eco_goods_id INTEGER")
                conn.commit()
            if "radar_last_matched_at" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN radar_last_matched_at DATETIME")
                conn.commit()
            if "priority_score" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_score FLOAT NOT NULL DEFAULT 0")
                conn.commit()
            if "priority_reason" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_reason VARCHAR(512)")
                conn.commit()
            if "priority_source" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_source VARCHAR(50)")
                conn.commit()
            if "priority_updated_at" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_updated_at DATETIME")
                conn.commit()
            if "priority_ttl_until" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_ttl_until DATETIME")
                conn.commit()
            if "priority_cooldown_until" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_cooldown_until DATETIME")
                conn.commit()
            if "priority_up_hits" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_up_hits INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            if "priority_down_hits" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN priority_down_hits INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            if "manual_watch" not in cols:
                conn.exec_driver_sql("ALTER TABLE item_base ADD COLUMN manual_watch BOOLEAN NOT NULL DEFAULT 0")
                conn.commit()
    except Exception:
        # 允许初始化阶段继续，避免因迁移失败阻塞整个系统启动
        pass


def _ensure_market_price_columns() -> None:
    """为已存在的 SQLite 库补齐 market_price 新增列。"""

    try:
        with engine.connect() as conn:
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(market_price)").all()}
            if "volume" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN volume INTEGER DEFAULT 0")
                conn.commit()
            if "sell_volume" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN sell_volume INTEGER")
                conn.commit()
            if "buy_volume" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN buy_volume INTEGER")
                conn.commit()
            if "orderbook_depth" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN orderbook_depth INTEGER")
                conn.commit()
            if "orderbook_balance" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN orderbook_balance FLOAT")
                conn.commit()
            if "liquidity_score" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN liquidity_score FLOAT")
                conn.commit()
            if "liquidity_source" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN liquidity_source VARCHAR(30)")
                conn.commit()
            if "currency" not in cols:
                conn.exec_driver_sql("ALTER TABLE market_price ADD COLUMN currency VARCHAR(10) NOT NULL DEFAULT 'CNY'")
                conn.commit()
    except Exception:
        pass


def _ensure_steamdt_opportunity_columns() -> None:
    """Lightweight migration guard for older SQLite databases."""

    try:
        with engine.connect() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='steamdt_opportunity'"
            ).fetchone()
            if not table:
                return
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(steamdt_opportunity)").all()}
            migrations = {
                "steam_sell_min": "ALTER TABLE steamdt_opportunity ADD COLUMN steam_sell_min FLOAT",
                "steam_buy_max": "ALTER TABLE steamdt_opportunity ADD COLUMN steam_buy_max FLOAT",
                "platform_sell_min": "ALTER TABLE steamdt_opportunity ADD COLUMN platform_sell_min FLOAT",
                "platform_buy_max": "ALTER TABLE steamdt_opportunity ADD COLUMN platform_buy_max FLOAT",
                "transaction_count_24h": "ALTER TABLE steamdt_opportunity ADD COLUMN transaction_count_24h INTEGER",
                "platform_sell_volume": "ALTER TABLE steamdt_opportunity ADD COLUMN platform_sell_volume INTEGER",
                "platform_buy_volume": "ALTER TABLE steamdt_opportunity ADD COLUMN platform_buy_volume INTEGER",
                "currency": "ALTER TABLE steamdt_opportunity ADD COLUMN currency VARCHAR(10) NOT NULL DEFAULT 'CNY'",
                "link_url": "ALTER TABLE steamdt_opportunity ADD COLUMN link_url VARCHAR(1024)",
                "steam_updated_at": "ALTER TABLE steamdt_opportunity ADD COLUMN steam_updated_at DATETIME",
                "platform_updated_at": "ALTER TABLE steamdt_opportunity ADD COLUMN platform_updated_at DATETIME",
                "steamdt_updated_at": "ALTER TABLE steamdt_opportunity ADD COLUMN steamdt_updated_at DATETIME",
            }
            for col, sql in migrations.items():
                if col not in cols:
                    conn.exec_driver_sql(sql)
                    conn.commit()
    except Exception:
        pass


def _ensure_arbitrage_opportunity_columns() -> None:
    try:
        with engine.connect() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='arbitrage_opportunity'"
            ).fetchone()
            if not table:
                return
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(arbitrage_opportunity)").all()}
            if "action" not in cols:
                conn.exec_driver_sql("ALTER TABLE arbitrage_opportunity ADD COLUMN action VARCHAR(50) NOT NULL DEFAULT 'direct_trade'")
                conn.commit()
    except Exception:
        pass


def _ensure_action_decision_columns() -> None:
    try:
        with engine.connect() as conn:
            table = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='action_decision'"
            ).fetchone()
            if not table:
                return
            cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(action_decision)").all()}
            migrations = {
                "opportunity_id": "ALTER TABLE action_decision ADD COLUMN opportunity_id INTEGER",
                "item_id": "ALTER TABLE action_decision ADD COLUMN item_id INTEGER NOT NULL DEFAULT 0",
                "action": "ALTER TABLE action_decision ADD COLUMN action VARCHAR(50) NOT NULL DEFAULT 'observe_only'",
                "target_platform": "ALTER TABLE action_decision ADD COLUMN target_platform VARCHAR(50) NOT NULL DEFAULT ''",
                "sell_platform": "ALTER TABLE action_decision ADD COLUMN sell_platform VARCHAR(50)",
                "target_price": "ALTER TABLE action_decision ADD COLUMN target_price FLOAT NOT NULL DEFAULT 0",
                "reference_price": "ALTER TABLE action_decision ADD COLUMN reference_price FLOAT",
                "quantity": "ALTER TABLE action_decision ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1",
                "score": "ALTER TABLE action_decision ADD COLUMN score FLOAT NOT NULL DEFAULT 0",
                "expected_profit_cny": "ALTER TABLE action_decision ADD COLUMN expected_profit_cny FLOAT NOT NULL DEFAULT 0",
                "expected_profit_rate": "ALTER TABLE action_decision ADD COLUMN expected_profit_rate FLOAT NOT NULL DEFAULT 0",
                "requires_jit": "ALTER TABLE action_decision ADD COLUMN requires_jit BOOLEAN NOT NULL DEFAULT 1",
                "status": "ALTER TABLE action_decision ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'open'",
                "reason": "ALTER TABLE action_decision ADD COLUMN reason VARCHAR(1024)",
                "risk_flags": "ALTER TABLE action_decision ADD COLUMN risk_flags VARCHAR(1024)",
                "created_at": "ALTER TABLE action_decision ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP",
                "updated_at": "ALTER TABLE action_decision ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
                "expires_at": "ALTER TABLE action_decision ADD COLUMN expires_at DATETIME",
            }
            for col, sql in migrations.items():
                if col not in cols:
                    conn.exec_driver_sql(sql)
                    conn.commit()
    except Exception:
        pass


def init_db() -> None:
    """创建全部表结构。"""

    Base.metadata.create_all(bind=engine)
    apply_schema_patches()
    _ensure_item_base_columns()
    _ensure_market_price_columns()
    _ensure_steamdt_opportunity_columns()
    _ensure_arbitrage_opportunity_columns()
    _ensure_action_decision_columns()
    print("Database schema initialized.")


if __name__ == "__main__":
    init_db()


if os.getenv("AETHERSWAP_SKIP_IMPORT_SCHEMA_PATCHES") != "1":
    apply_schema_patches()


def normalize_data_timestamp(value: datetime | str | None = None) -> datetime:
    """Normalize source data timestamps to the naive local DateTime used by SQLite."""

    if value is None:
        return datetime.now()

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return datetime.now()
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("无法解析数据时间戳，回退到当前时间 | value=%s", value)
            return datetime.now()

    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def is_fresh_enough(current_updated_at: datetime | None, new_timestamp: datetime | str | None) -> bool:
    """Return True only when incoming data is at least as fresh as the stored row."""

    if current_updated_at is None:
        return True
    incoming = normalize_data_timestamp(new_timestamp)
    current = normalize_data_timestamp(current_updated_at)
    return incoming >= current


def upsert_market_price_if_fresh(
    db,
    *,
    item_id: int,
    platform_name: str,
    data_source: str,
    sell_min: float | None = None,
    sell_top5_avg: float | None = None,
    buy_max: float | None = None,
    buy_top5_avg: float | None = None,
    volume: int | None = None,
    sell_volume: int | None = None,
    buy_volume: int | None = None,
    orderbook_depth: int | None = None,
    orderbook_balance: float | None = None,
    liquidity_score: float | None = None,
    liquidity_source: str | None = None,
    currency: str = "CNY",
    new_timestamp: datetime | str | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Upsert one MarketPrice row, blocking stale source data from overwriting newer rows."""

    log = log or logger
    incoming_ts = normalize_data_timestamp(new_timestamp)
    price_record = (
        db.query(MarketPrice)
        .filter(MarketPrice.item_id == item_id, MarketPrice.platform_name == platform_name)
        .first()
    )

    if price_record and not is_fresh_enough(price_record.updated_at, incoming_ts):
        log.debug(
            "旧数据拦截 | item_id=%s platform=%s source=%s incoming=%s current=%s",
            item_id,
            platform_name,
            data_source,
            incoming_ts,
            price_record.updated_at,
        )
        return False

    values = {
        "data_source": data_source,
        "currency": currency or "CNY",
        "updated_at": incoming_ts,
    }
    optional_values = {
        "sell_min": sell_min,
        "sell_top5_avg": sell_top5_avg,
        "buy_max": buy_max,
        "buy_top5_avg": buy_top5_avg,
        "volume": volume,
        "sell_volume": sell_volume,
        "buy_volume": buy_volume,
        "orderbook_depth": orderbook_depth,
        "orderbook_balance": orderbook_balance,
        "liquidity_score": liquidity_score,
        "liquidity_source": liquidity_source,
    }
    values.update({key: value for key, value in optional_values.items() if value is not None})

    if price_record:
        for key, value in values.items():
            if hasattr(price_record, key):
                setattr(price_record, key, value)
    else:
        db.add(
            MarketPrice(
                item_id=item_id,
                platform_name=platform_name,
                **{key: value for key, value in values.items() if hasattr(MarketPrice, key)},
            )
        )
    return True
