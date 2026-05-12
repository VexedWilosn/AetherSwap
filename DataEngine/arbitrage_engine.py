from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from DataEngine.database import ArbitrageOpportunity, MarketPrice, SessionLocal
from DataEngine.profit_model import get_after_tax_price, opportunity_profit, steam_balance_cost_ratio

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "aetherswap_engine.log"
DB_PATH = BASE_DIR / "config" / "market_data.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_root_logger = logging.getLogger()
if not _root_logger.handlers:
    _root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    _root_logger.addHandler(console_handler)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    _root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

APP_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
SUPPORTED_EXECUTOR_BUY_PLATFORMS = {"buff", "uuyp", "eco", "steam"}


@dataclass(frozen=True)
class ProfitResult:
    buy_price: float
    sell_price: float
    buy_platform: str
    sell_platform: str
    after_tax_revenue: float
    self_profit: float
    cash_profit: float
    discount: float
    profit_rate: float


def load_pipeline_config() -> dict[str, Any]:
    try:
        if APP_CONFIG_PATH.exists():
            return json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8") or "{}") or {}
    except Exception as exc:
        logger.warning("读取 app_config.json 失败: %s", exc)
    return {}


class ArbitrageScanner:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def _load_latest_prices(self, session: Session) -> dict[int, dict[str, MarketPrice]]:
        rows = session.query(MarketPrice).all()
        latest_prices: dict[int, dict[str, MarketPrice]] = {}
        for row in rows:
            latest_prices.setdefault(row.item_id, {})[row.platform_name.lower().strip()] = row
        return latest_prices

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_non_decision_price(row: MarketPrice | Any) -> bool:
        return str(getattr(row, "data_source", "") or "").strip().lower() == "baseline"

    def _pick_platform_price(self, row: MarketPrice, platform: str) -> float:
        key = (platform or "").lower().strip()
        if key == "steam":
            return self._safe_float(row.sell_min)
        return self._safe_float(row.buy_max) if self._safe_float(row.buy_max) > 0 else self._safe_float(row.sell_min)

    def _build_opportunity(
        self,
        item_id: int,
        item_name: str,
        buy_platform: str,
        sell_platform: str,
        buy_price: float,
        sell_price: float,
        balance_cost_ratio: float,
        max_discount: float,
    ) -> Optional[dict[str, Any]]:
        if buy_price <= 0 or sell_price <= 0:
            return None

        math = opportunity_profit(
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            buy_price=buy_price,
            sell_price=sell_price,
            balance_cost_ratio=balance_cost_ratio,
        )
        after_tax_revenue = math.revenue_cny
        self_profit = after_tax_revenue - buy_price
        cash_profit = math.profit_cny
        profit_rate = math.profit_rate

        if cash_profit <= 0 and self_profit <= 0:
            return None
        effective_cost = math.cost_cny
        discount = effective_cost / sell_price if sell_price > 0 else 999.0
        if (buy_platform or "").lower().strip() != "steam" and discount > max_discount:
            return None

        return {
            "item_id": item_id,
            "buy_platform": buy_platform,
            "buy_price": buy_price,
            "sell_platform": sell_platform,
            "sell_price": sell_price,
            "profit_cny": cash_profit,
            "profit_rate": profit_rate,
            "action": "direct_trade",
            "status": "open",
            "updated_at": datetime.now(UTC),
        }

    def scan_opportunities(self) -> list[dict[str, Any]]:
        cfg = load_pipeline_config()
        pipeline_cfg = cfg.get("pipeline", {}) if isinstance(cfg, dict) else {}
        balance_ratio = steam_balance_cost_ratio(cfg)
        max_discount = float(pipeline_cfg.get("max_discount", 0.8) or 0.8)

        session: Session = self.session_factory()
        try:
            latest_prices = self._load_latest_prices(session)
            opportunities: list[dict[str, Any]] = []

            for item_id, price_map in latest_prices.items():
                steam_record = price_map.get("steam")
                if not steam_record or self._is_non_decision_price(steam_record):
                    continue

                steam_price = self._safe_float(steam_record.sell_min)
                if steam_price <= 0:
                    continue

                tp_records = {
                    k: v
                    for k, v in price_map.items()
                    if k != "steam" and not self._is_non_decision_price(v)
                }
                if not tp_records:
                    continue

                best_tp_buy_plat = None
                best_tp_buy_price = None
                best_tp_sell_plat = None
                best_tp_sell_price = None

                for plat, row in tp_records.items():
                    buy_price = self._safe_float(row.sell_min)
                    sell_price = self._safe_float(row.buy_max) if self._safe_float(row.buy_max) > 0 else self._safe_float(row.sell_min)

                    if buy_price > 0 and (best_tp_buy_price is None or buy_price < best_tp_buy_price):
                        best_tp_buy_price = buy_price
                        best_tp_buy_plat = plat

                    if sell_price > 0 and (best_tp_sell_price is None or sell_price > best_tp_sell_price):
                        best_tp_sell_price = sell_price
                        best_tp_sell_plat = plat

                if best_tp_buy_plat in SUPPORTED_EXECUTOR_BUY_PLATFORMS and best_tp_buy_price:
                    opp_a = self._build_opportunity(
                        item_id=item_id,
                        item_name=str(item_id),
                        buy_platform=best_tp_buy_plat,
                        sell_platform="steam",
                        buy_price=best_tp_buy_price,
                        sell_price=steam_price,
                        balance_cost_ratio=balance_ratio,
                        max_discount=max_discount,
                    )
                    if opp_a:
                        opportunities.append(opp_a)

                if best_tp_sell_plat and best_tp_sell_price:
                    opp_b = self._build_opportunity(
                        item_id=item_id,
                        item_name=str(item_id),
                        buy_platform="steam",
                        sell_platform=best_tp_sell_plat,
                        buy_price=steam_price,
                        sell_price=best_tp_sell_price,
                        balance_cost_ratio=balance_ratio,
                        max_discount=max_discount,
                    )
                    if opp_b:
                        opportunities.append(opp_b)

            self._close_non_decision_opportunities(session, latest_prices)
            self._bulk_upsert(session, opportunities)
            session.commit()
            logger.info("扫描完成，共发现 %s 个有效机会", len(opportunities))
            return opportunities
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _close_non_decision_opportunities(
        self,
        session: Session,
        latest_prices: dict[int, dict[str, MarketPrice]],
    ) -> int:
        invalid_platforms_by_item = {
            int(item_id): {
                str(platform or "").lower().strip()
                for platform, row in price_map.items()
                if self._is_non_decision_price(row)
            }
            for item_id, price_map in latest_prices.items()
        }
        invalid_platforms_by_item = {
            item_id: platforms
            for item_id, platforms in invalid_platforms_by_item.items()
            if platforms
        }
        if not invalid_platforms_by_item:
            return 0

        rows = (
            session.query(ArbitrageOpportunity)
            .filter(ArbitrageOpportunity.status.in_(["open", "verifying"]))
            .all()
        )
        closed = 0
        now = datetime.now(UTC)
        for row in rows:
            invalid_platforms = invalid_platforms_by_item.get(int(row.item_id or 0))
            if not invalid_platforms:
                continue
            buy_platform = str(row.buy_platform or "").lower().strip()
            sell_platform = str(row.sell_platform or "").lower().strip()
            if buy_platform not in invalid_platforms and sell_platform not in invalid_platforms:
                continue
            row.status = "closed"
            row.updated_at = now
            session.add(row)
            closed += 1
        if closed:
            logger.info("closed %s opportunities backed by non-decision baseline prices", closed)
        return closed

    def _bulk_upsert(self, session: Session, opportunities: list[dict[str, Any]]) -> None:
        if not opportunities:
            return

        stmt = sqlite_insert(ArbitrageOpportunity).values(opportunities)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ArbitrageOpportunity.item_id, ArbitrageOpportunity.buy_platform, ArbitrageOpportunity.sell_platform],
            set_={
                "buy_price": stmt.excluded.buy_price,
                "sell_price": stmt.excluded.sell_price,
                "profit_cny": stmt.excluded.profit_cny,
                "profit_rate": stmt.excluded.profit_rate,
                "action": stmt.excluded.action,
                "status": stmt.excluded.status,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        session.execute(stmt)


def scan_opportunities() -> list[dict[str, Any]]:
    return ArbitrageScanner().scan_opportunities()


if __name__ == "__main__":
    scan_opportunities()
