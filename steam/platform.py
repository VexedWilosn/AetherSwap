from __future__ import annotations

from typing import Any, Optional

from app.services.market_platform import BaseMarketPlatform

from .client import fetch_history, market_hash_name_from_listing_url
from .inventory import fetch_cs2_inventory, fetch_inventory
from .market import list_item_by_name


class SteamMarketPlatform(BaseMarketPlatform):
    def __init__(self, session, steam_id: str, session_id: str) -> None:
        self.session = session
        self.steam_id = steam_id
        self.session_id = session_id

    def get_price(self, market_hash_name: str, app_id: int = 730, return_currency: bool = False):
        return fetch_history(market_hash_name, app_id=app_id, return_currency=return_currency)

    def buy(self, *args: Any, **kwargs: Any) -> Any:
        return list_item_by_name(self.session, self.steam_id, self.session_id, *args, **kwargs)

    def sell(self, item_name: str, price: float, *, app_id: int = 753, context_id: int = 6, count: int = 75):
        return list_item_by_name(
            self.session,
            self.steam_id,
            self.session_id,
            item_name,
            price,
            app_id=app_id,
            context_id=context_id,
            count=count,
        )

    def query_inventory(self, app_id: int = 730, context_id: int = 6, *, count: int = 75):
        if app_id == 730:
            return fetch_cs2_inventory(self.session, self.steam_id, count=count)
        return fetch_inventory(self.session, self.steam_id, app_id=app_id, context_id=context_id, count=count)
