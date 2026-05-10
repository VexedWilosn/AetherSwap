import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional
from sqlmodel import Field, Session, SQLModel, select
from sqlalchemy import Index, inspect, text as sa_text
from DataEngine.database import (
    ArbitrageOpportunity,
    Base as DataEngineBase,
    ItemBase,
    MarketPrice,
    PlatformMapping,
    SessionLocal,
    SteamDTOpportunity,
    engine,
)
from DataEngine.sqlite_pragmas import install_sqlite_pragmas
BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = BASE_DIR / "config"
_DB_PATH = _CONFIG_DIR / "market_data.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_TRANSACTIONS_JSON = _CONFIG_DIR / "transactions.json"
_TRANSACTIONS_BAK = _CONFIG_DIR / "transactions.json.bak"
_WILSON_Z = 1.96
def _compute_wilson_score(positive_rate, total_reviews):
    # Wilson Score 置信下界，review少的游戏即使满分也会被降权
    # 参考: https://www.evanmiller.org/how-not-to-sort-by-average-rating.html
    n = total_reviews or 0
    if n <= 0 or positive_rate is None:
        return 0.0
    p = positive_rate / 100.0
    z = _WILSON_Z
    z2 = z * z
    numerator = p + z2 / (2 * n) - z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    denominator = 1 + z2 / n
    return max(0.0, numerator / denominator)
class Purchase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    goods_id: int = 0
    price: float = 0.0
    at: float = 0.0
    market_price: Optional[float] = None
    sale_price: Optional[float] = None
    sold_at: Optional[float] = None
    pending_receipt: Optional[bool] = None
    assetid: Optional[str] = None
    listing: Optional[bool] = None
    listing_status: Optional[str] = None
class Sale(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    goods_id: int = 0
    price: float = 0.0
    at: float = 0.0
    assetid: Optional[str] = None
class ItemNameId(SQLModel, table=True):
    market_hash_name: str = Field(primary_key=True)
    item_nameid: str
class SteamDealGame(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: str = Field(index=True, unique=True)
    name: str = ""
    name_en: str = ""
    banner_url: str = ""
    positive_rate: Optional[float] = None   
    total_reviews: int = 0
    discount_percent: int = 0               
    deal_status: Optional[str] = None       
    price_cn: Optional[str] = None
    price_ru: Optional[str] = None
    price_kz: Optional[str] = None
    price_ua: Optional[str] = None
    price_pk: Optional[str] = None
    price_tr: Optional[str] = None
    price_ar: Optional[str] = None
    price_az: Optional[str] = None
    price_vn: Optional[str] = None
    price_id: Optional[str] = None
    price_in: Optional[str] = None
    price_br: Optional[str] = None
    price_cl: Optional[str] = None
    price_jp: Optional[str] = None
    price_hk: Optional[str] = None
    price_ph: Optional[str] = None
    original_cn: Optional[str] = None
    discount_cn: Optional[str] = None
    discount_ru: Optional[str] = None
    discount_kz: Optional[str] = None
    discount_ua: Optional[str] = None
    discount_pk: Optional[str] = None
    discount_tr: Optional[str] = None
    discount_ar: Optional[str] = None
    discount_az: Optional[str] = None
    discount_vn: Optional[str] = None
    discount_id: Optional[str] = None
    discount_in: Optional[str] = None
    discount_br: Optional[str] = None
    discount_cl: Optional[str] = None
    discount_jp: Optional[str] = None
    discount_hk: Optional[str] = None
    discount_ph: Optional[str] = None
    fetched_at: float = 0.0                 
    wilson_score: Optional[float] = None    


class TradeExecutionRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: float = Field(default=0.0, index=True)
    action: str = Field(default="", index=True)
    channel: str = Field(default="", index=True)
    item_id: int = Field(default=0, index=True)
    market_hash_name: str = Field(default="")
    platform: str = Field(default="", index=True)
    quantity: int = 1
    target_price: Optional[float] = None
    reference_price: Optional[float] = None
    status: str = Field(default="queued", index=True)
    request_payload: Optional[str] = None
    response_payload: Optional[str] = None
    error_message: Optional[str] = None


class PlatformAction(SQLModel, table=True):
    __tablename__ = "platform_action"
    __table_args__ = (
        Index("ix_platform_action_state_next_check_at", "state", "next_check_at"),
        Index("ix_platform_action_platform_state_next_check_at", "platform", "state", "next_check_at"),
        Index("ix_platform_action_item_state", "item_id", "state"),
        Index("ix_platform_action_risk_category_state", "risk_category", "state"),
        Index("ix_platform_action_platform_order_id", "platform", "platform_order_id"),
        Index("ix_platform_action_trade_offer_id", "trade_offer_id"),
        Index("ix_platform_action_assetid", "assetid"),
        Index("ix_platform_action_idempotency_key", "idempotency_key", unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    finished_at: Optional[float] = None
    next_check_at: float = Field(default_factory=time.time)
    lease_until: Optional[float] = None

    action_type: str = Field(default="", max_length=64)
    platform: str = Field(default="", max_length=50)
    state: str = Field(default="queued", max_length=40)
    channel: str = Field(default="auto", max_length=40)

    item_id: int = 0
    market_hash_name: str = Field(default="", max_length=255)
    risk_category: str = Field(default="", max_length=255)
    quantity: int = 1
    target_price: Optional[float] = None
    reference_price: Optional[float] = None
    cost_basis_cny: Optional[float] = None
    expected_profit_rate: Optional[float] = None
    locked_budget_cny: float = 0.0
    filled_quantity: int = 0
    remaining_quantity: Optional[int] = None
    filled_amount_cny: float = 0.0
    released_budget_cny: float = 0.0

    platform_order_id: Optional[str] = Field(default=None, max_length=128)
    platform_listing_id: Optional[str] = Field(default=None, max_length=128)
    trade_offer_id: Optional[str] = Field(default=None, max_length=128)
    assetid: Optional[str] = Field(default=None, max_length=128)
    idempotency_key: Optional[str] = Field(default=None, max_length=255)

    retry_count: int = 0
    max_retries: int = 3
    error_code: Optional[str] = Field(default=None, max_length=80)
    error_message: Optional[str] = None
    request_payload: Optional[str] = None
    response_payload: Optional[str] = None
    raw_context: Optional[str] = None

_engine = None
_engine_lock = threading.Lock()

def get_engine():
    install_sqlite_pragmas(engine)
    return engine

def get_session() -> Session:
    return SessionLocal()

def _create_all_app_tables() -> None:
    """确保 app 层的 SQLModel 表结构在空库时也会被创建。"""
    SQLModel.metadata.create_all(bind=engine)


def _ensure_wilson_score_column() -> None:
    from sqlalchemy import text as sa_text

    with engine.connect() as conn:
        try:
            conn.execute(sa_text("ALTER TABLE steamdealgame ADD COLUMN wilson_score REAL"))
            conn.commit()
        except Exception:
            pass


def _ensure_platform_action_partial_fill_columns() -> None:
    columns = {
        "risk_category": "VARCHAR(255) NOT NULL DEFAULT ''",
        "filled_quantity": "INTEGER NOT NULL DEFAULT 0",
        "remaining_quantity": "INTEGER",
        "filled_amount_cny": "FLOAT NOT NULL DEFAULT 0",
        "released_budget_cny": "FLOAT NOT NULL DEFAULT 0",
    }
    with engine.connect() as conn:
        try:
            inspector = inspect(conn)
            if "platform_action" not in inspector.get_table_names():
                return
            existing = {col["name"] for col in inspector.get_columns("platform_action")}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(sa_text(f"ALTER TABLE platform_action ADD COLUMN {name} {ddl}"))
            conn.execute(
                sa_text(
                    "CREATE INDEX IF NOT EXISTS ix_platform_action_risk_category_state "
                    "ON platform_action (risk_category, state)"
                )
            )
            conn.commit()
        except Exception:
            pass


def _backfill_wilson_scores() -> None:
    from sqlalchemy import text as sa_text

    with engine.connect() as conn:
        table_exists = conn.execute(
            sa_text("SELECT name FROM sqlite_master WHERE type='table' AND name='steamdealgame'")
        ).fetchone()
        if not table_exists:
            return

        try:
            rows = conn.execute(
                sa_text("SELECT id, positive_rate, total_reviews FROM steamdealgame WHERE wilson_score IS NULL")
            ).fetchall()
        except Exception:
            # 表存在但迁移尚未就绪时直接跳过回填，避免启动阶段崩溃
            return

        if not rows:
            return

        for row in rows:
            ws = _compute_wilson_score(row[1], row[2])
            conn.execute(
                sa_text("UPDATE steamdealgame SET wilson_score = :ws WHERE id = :id"),
                {"ws": ws, "id": row[0]},
            )
        conn.commit()


def init_db() -> None:
    """创建全部表结构，并执行必要的轻量迁移。"""
    DataEngineBase.metadata.create_all(bind=engine)
    _create_all_app_tables()
    _ensure_wilson_score_column()
    _ensure_platform_action_partial_fill_columns()
    _backfill_wilson_scores()
    try:
        SQLModel.metadata.create_all(bind=engine)
    except Exception:
        pass
    try:
        db_file = Path(__file__).resolve().parent.parent / "config" / "market_data.db"
        with sqlite3.connect(str(db_file), timeout=10) as conn:
            conn.execute("ALTER TABLE item_base ADD COLUMN radar_last_matched_at DATETIME")
            print("✅ 原生 SQLite 成功添加 radar_last_matched_at 字段")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            pass
        else:
            print(f"⚠️ 原生 SQL 补丁跳过: {e}")
    except Exception as e:
        print(f"⚠️ 补丁发生异常: {e}")
def _purchase_from_dict(d: dict) -> Purchase:
    return Purchase(
        name=d.get("name", ""),
        goods_id=int(d.get("goods_id", 0) or 0),
        price=float(d.get("price", 0)),
        at=float(d.get("at", 0)),
        market_price=float(d["market_price"]) if d.get("market_price") is not None else None,
        sale_price=float(d["sale_price"]) if d.get("sale_price") is not None else None,
        sold_at=float(d["sold_at"]) if d.get("sold_at") is not None else None,
        pending_receipt=bool(d["pending_receipt"]) if d.get("pending_receipt") is not None else None,
        assetid=str(d["assetid"]) if d.get("assetid") is not None else None,
        listing=bool(d["listing"]) if d.get("listing") is not None else None,
        listing_status=str(d["listing_status"]) if d.get("listing_status") is not None else None,
    )
def _sale_from_dict(d: dict) -> Sale:
    return Sale(
        name=d.get("name", ""),
        goods_id=int(d.get("goods_id", 0) or 0),
        price=float(d.get("price", 0)),
        at=float(d.get("at", 0)),
        assetid=str(d["assetid"]) if d.get("assetid") is not None else None,
    )
def _purchase_to_dict(p: Purchase) -> dict:
    d = {
        "_db_id": p.id,  
        "name": p.name,
        "goods_id": p.goods_id,
        "price": p.price,
        "at": p.at,
    }
    if p.market_price is not None:
        d["market_price"] = p.market_price
    if p.sale_price is not None:
        d["sale_price"] = p.sale_price
    if p.sold_at is not None:
        d["sold_at"] = p.sold_at
    if p.pending_receipt is not None:
        d["pending_receipt"] = p.pending_receipt
    if p.assetid is not None:
        d["assetid"] = p.assetid
    if p.listing is not None:
        d["listing"] = p.listing
    if p.listing_status is not None:
        d["listing_status"] = p.listing_status
    return d
def _sale_to_dict(s: Sale) -> dict:
    d = {
        "name": s.name,
        "goods_id": s.goods_id,
        "price": s.price,
        "at": s.at,
    }
    if s.assetid is not None:
        d["assetid"] = s.assetid
    return d
def migrate_from_json() -> bool:
    """迁移基础饰品数据到统一的 market_data.db。"""
    items_json = _CONFIG_DIR / "items.json"
    if not items_json.exists():
        return False

    count = 0
    with get_session() as session:
        try:
            data = json.loads(items_json.read_text(encoding="utf-8") or "[]")
            if not isinstance(data, list):
                return False

            for row in data:
                if not isinstance(row, dict):
                    continue
                market_hash_name = str(row.get("market_hash_name", "")).strip()
                if not market_hash_name:
                    continue
                item = session.execute(select(ItemBase).where(ItemBase.market_hash_name == market_hash_name)).scalars().first()
                if item is None:
                    item = ItemBase(
                        market_hash_name=market_hash_name,
                        cn_name=row.get("cn_name"),
                        buff_goods_id=int(row["buff_goods_id"]) if row.get("buff_goods_id") is not None else None,
                        uuyp_template_id=int(row["uuyp_template_id"]) if row.get("uuyp_template_id") is not None else None,
                        eco_goods_id=int(row["eco_goods_id"]) if row.get("eco_goods_id") is not None else None,
                        game=str(row.get("game", "csgo")),
                    )
                    session.add(item)
                    count += 1
                else:
                    changed = False
                    for key in ("cn_name", "buff_goods_id", "uuyp_template_id", "eco_goods_id", "game"):
                        value = row.get(key)
                        if key.endswith("_id") and value is not None:
                            value = int(value)
                        if value is not None and getattr(item, key, None) != value:
                            setattr(item, key, value)
                            changed = True
                    if changed:
                        session.add(item)
                        count += 1
            session.commit()
        except Exception:
            session.rollback()
            raise

    print(f"数据迁移成功，共写入 {count} 条记录至 {_DB_PATH}")
    return count > 0
_PURCHASE_UPDATABLE = frozenset({
    "name", "price", "goods_id", "market_price", "sale_price",
    "sold_at", "pending_receipt", "assetid", "listing", "listing_status",
})
_SALE_UPDATABLE = frozenset({"name", "price", "goods_id", "assetid", "at"})
def db_append_purchase(p: dict) -> None:
    with get_session() as session:
        session.add(_purchase_from_dict(p))
        session.commit()
def db_get_purchases() -> list:
    with get_session() as session:
        rows = session.execute(select(Purchase).order_by(Purchase.id)).scalars().all()
        return [_purchase_to_dict(r) for r in rows]
def db_append_sale(s: dict) -> None:
    with get_session() as session:
        session.add(_sale_from_dict(s))
        session.commit()
def db_get_sales() -> list:
    with get_session() as session:
        rows = session.execute(select(Sale).order_by(Sale.id)).scalars().all()
        return [_sale_to_dict(r) for r in rows]
def db_clear_transactions() -> None:
    from sqlmodel import delete as sql_delete
    with get_session() as session:
        session.execute(sql_delete(Purchase))
        session.execute(sql_delete(Sale))
        session.commit()
def db_replace_transactions(purchases: list, sales: list) -> None:
    from sqlmodel import delete as sql_delete
    with get_session() as session:
        session.execute(sql_delete(Purchase))
        session.execute(sql_delete(Sale))
        for p in purchases:
            session.add(_purchase_from_dict(p))
        for s in sales:
            session.add(_sale_from_dict(s))
        session.commit()
def db_delete_purchase(idx: int) -> bool:
    """Delete purchase by positional index (0-based, ordered by id)."""
    with get_session() as session:
        rows = session.execute(select(Purchase).order_by(Purchase.id)).scalars().all()
        if 0 <= idx < len(rows):
            session.delete(rows[idx])
            session.commit()
            return True
    return False
def db_delete_sale(idx: int) -> bool:
    with get_session() as session:
        rows = session.execute(select(Sale).order_by(Sale.id)).scalars().all()
        if 0 <= idx < len(rows):
            session.delete(rows[idx])
            session.commit()
            return True
    return False
def db_update_purchase(idx: int, data: dict) -> bool:
    """按位置索引更新（兼容旧接口，UI 路由使用）。"""
    with get_session() as session:
        rows = session.execute(select(Purchase).order_by(Purchase.id)).scalars().all()
        if 0 <= idx < len(rows):
            row = rows[idx]
            for k, v in data.items():
                if k in _PURCHASE_UPDATABLE:
                    setattr(row, k, v)
            session.add(row)
            session.commit()
            return True
    return False
def db_update_purchase_by_id(db_id: int, data: dict) -> bool:
    """按主键 ID 更新，O(1) 操作，推荐内部 worker 使用。"""
    if not db_id:
        return False
    with get_session() as session:
        row = session.get(Purchase, db_id)
        if row is None:
            return False
        for k, v in data.items():
            if k in _PURCHASE_UPDATABLE:
                setattr(row, k, v)
        session.add(row)
        session.commit()
        return True
def db_delete_purchase_by_id(db_id: int) -> bool:
    """按主键 ID 删除，O(1) 操作。"""
    if not db_id:
        return False
    with get_session() as session:
        row = session.get(Purchase, db_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
def db_update_sale(idx: int, data: dict) -> bool:
    with get_session() as session:
        rows = session.execute(select(Sale).order_by(Sale.id)).scalars().all()
        if 0 <= idx < len(rows):
            row = rows[idx]
            for k, v in data.items():
                if k in _SALE_UPDATABLE:
                    setattr(row, k, v)
            session.add(row)
            session.commit()
            return True
    return False
def db_delete_sale_by_id(db_id: int) -> bool:
    """Delete by primary ID, O(1) operation."""
    if not db_id:
        return False
    with get_session() as session:
        row = session.get(Sale, db_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
def db_get_item_nameid(market_hash_name: str) -> Optional[str]:
    with get_session() as session:
        item = session.execute(
            select(ItemNameId).where(ItemNameId.market_hash_name == market_hash_name)
        ).scalars().first()
        return item.item_nameid if item else None
def db_set_item_nameid(market_hash_name: str, item_nameid: str) -> None:
    with get_session() as session:
        item = session.execute(
            select(ItemNameId).where(ItemNameId.market_hash_name == market_hash_name)
        ).scalars().first()
        if item:
            item.item_nameid = item_nameid
        else:
            item = ItemNameId(market_hash_name=market_hash_name, item_nameid=item_nameid)
        session.add(item)
        session.commit()
_REGION_CODES = [
    "cn", "ru", "kz", "ua", "pk", "tr", "ar", "az",
    "vn", "id", "in", "br", "cl", "jp", "hk", "ph",
]
def _game_row_to_dict(r: SteamDealGame) -> dict:  # Refactored: was copy-pasted verbatim into both db_get_steam_deals and db_get_steam_deals_by_app_ids
    d = {
        "app_id": r.app_id,
        "name": r.name,
        "name_en": r.name_en,
        "banner_url": r.banner_url,
        "positive_rate": r.positive_rate,
        "total_reviews": r.total_reviews,
        "discount_percent": r.discount_percent,
        "deal_status": r.deal_status,
        "fetched_at": r.fetched_at,
        "prices": {},
        "discounts": {},
        "original_cn": r.original_cn,
    }
    for rc in _REGION_CODES:
        d["prices"][rc] = getattr(r, f"price_{rc}", None)
        d["discounts"][rc] = getattr(r, f"discount_{rc}", None)
    return d
def db_upsert_steam_deal(data: dict) -> None:
    """Insert or update a SteamDealGame by app_id."""
    data = dict(data)  
    data["wilson_score"] = _compute_wilson_score(
        data.get("positive_rate"), data.get("total_reviews")
    )
    with get_session() as session:
        existing = session.execute(
            select(SteamDealGame).where(SteamDealGame.app_id == str(data["app_id"]))
        ).scalars().first()
        if existing:
            for k, v in data.items():
                if k != "id" and hasattr(existing, k):
                    setattr(existing, k, v)
            session.add(existing)
        else:
            game = SteamDealGame(**{k: v for k, v in data.items() if hasattr(SteamDealGame, k)})
            session.add(game)
        session.commit()
def db_get_steam_deals(
    offset: int = 0,
    limit: int = 30,
    search: str = "",
    sort_by: str = "discount_percent",
    sort_dir: str = "asc",
    compare_region: str = "",
    deal_status_filter: str = "",
) -> list:
    """Paginated query with optional search and sorting."""
    from sqlmodel import col, text as sql_text, or_
    with get_session() as session:
        stmt = select(SteamDealGame)
        if search:
            stmt = stmt.where(
                or_(
                    col(SteamDealGame.name).contains(search),
                    col(SteamDealGame.name_en).contains(search)
                )
            )
        if deal_status_filter and deal_status_filter != "全部状态":
            stmt = stmt.where(SteamDealGame.deal_status == deal_status_filter)
        order_col = None
        if sort_by == "positive_rate":
            order_col = SteamDealGame.positive_rate
        elif sort_by == "total_reviews":
            order_col = SteamDealGame.total_reviews
        elif sort_by == "discount_percent":
            order_col = SteamDealGame.discount_percent
        elif sort_by == "name":
            order_col = SteamDealGame.name
        elif sort_by in ("default_recommend", "price_diff", "discount_abs", "region_value"):
            # price_diff/discount_abs 这俩路由层已经走内存排序了
            # 这里是 search+filter 组合时的回退，必须加分页防止全表返回
            stmt = stmt.order_by(col(SteamDealGame.wilson_score).desc())
            stmt = stmt.offset(offset).limit(limit)
        else:
            stmt = stmt.order_by(col(SteamDealGame.wilson_score).desc())
            stmt = stmt.offset(offset).limit(limit)
        if order_col is not None:
            if sort_dir == "desc":
                stmt = stmt.order_by(col(order_col).desc())
            else:
                stmt = stmt.order_by(col(order_col).asc())
            stmt = stmt.offset(offset).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [_game_row_to_dict(r) for r in rows]
def db_get_steam_deals_count(search: str = "") -> int:
    from sqlmodel import col, func, or_
    with get_session() as session:
        stmt = select(func.count()).select_from(SteamDealGame)
        if search:
            stmt = stmt.where(
                or_(
                    col(SteamDealGame.name).contains(search),
                    col(SteamDealGame.name_en).contains(search)
                )
            )
        return session.execute(stmt).scalar_one()
def db_get_steam_deals_last_update() -> Optional[float]:
    from sqlmodel import func
    with get_session() as session:
        result = session.execute(
            select(func.max(SteamDealGame.fetched_at))
        ).scalars().first()
        return result if result else None
def db_clear_steam_deals() -> None:
    from sqlmodel import delete as sql_delete
    with get_session() as session:
        session.execute(sql_delete(SteamDealGame))
        session.commit()
def db_get_steam_deals_price_snapshot() -> list:
    """Lightweight fetch: only price-related columns for ALL games.
    Used to build an in-memory sort index (price_diff / discount_abs) without
    the cost of fetching every column for 20 000+ rows. Returns a list of
    plain dicts with keys: app_id, original_cn, price_<cc> for each region.
    """
    from sqlalchemy import text as sa_text
    price_cols = ", ".join(["app_id", "original_cn"] + [f"price_{rc}" for rc in _REGION_CODES])
    with get_engine().connect() as conn:
        rows = conn.execute(sa_text(f"SELECT {price_cols} FROM steamdealgame")).fetchall()
    result = []
    for row in rows:
        d = {"app_id": row[0], "original_cn": row[1]}
        for i, rc in enumerate(_REGION_CODES):
            d[f"price_{rc}"] = row[2 + i]
        result.append(d)
    return result
def db_get_steam_deals_review_snapshot() -> list:
    """Lightweight fetch: only app_id and total_reviews for ALL games.
    Used to filter games with >= 2000 reviews for region_value sort mode.
    """
    from sqlalchemy import text as sa_text
    with get_engine().connect() as conn:
        rows = conn.execute(sa_text("SELECT app_id, total_reviews FROM steamdealgame")).fetchall()
    return [{"app_id": row[0], "total_reviews": row[1]} for row in rows]
def db_get_steam_deals_by_app_ids(app_ids: List[str]) -> list:
    """Fetch full game data for a specific ordered list of app_ids.
    Only fetches the rows listed in app_ids and preserves the given order.
    Used after the sort index resolves which 30 games to show on this page.
    """
    if not app_ids:
        return []
    from sqlmodel import col
    with get_session() as session:
        rows = session.execute(
            select(SteamDealGame).where(col(SteamDealGame.app_id).in_(app_ids))
        ).scalars().all()
        id_to_row = {r.app_id: _game_row_to_dict(r) for r in rows}
        return [id_to_row[aid] for aid in app_ids if aid in id_to_row]
