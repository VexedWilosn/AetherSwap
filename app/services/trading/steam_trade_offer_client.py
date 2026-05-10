from __future__ import annotations

import os
from typing import Any

import requests


STEAM_TRADE_OFFER_URL = "https://api.steampowered.com/IEconService/GetTradeOffer/v1/"


def steam_web_api_key_from_sources(
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    credentials = credentials or {}
    config = config or {}
    steam = credentials.get("steam") if isinstance(credentials.get("steam"), dict) else {}
    steam_cfg = config.get("steam") if isinstance(config.get("steam"), dict) else {}
    return str(
        os.getenv("STEAM_WEB_API_KEY", "")
        or steam.get("web_api_key")
        or steam.get("api_key")
        or steam.get("steam_web_api_key")
        or steam_cfg.get("web_api_key")
        or steam_cfg.get("api_key")
        or config.get("STEAM_WEB_API_KEY")
        or ""
    ).strip()


class SteamTradeOfferClient:
    def __init__(self, api_key: str, *, timeout: int = 15, session: Any | None = None):
        self.api_key = str(api_key or "").strip()
        self.timeout = int(timeout or 15)
        self.session = session or requests.Session()

    def fetch_offer(self, trade_offer_id: str) -> dict[str, Any]:
        offer_id = str(trade_offer_id or "").strip()
        if not offer_id:
            return {"success": False, "reason": "trade_offer_id_required", "msg": "trade_offer_id is required"}
        if not self.api_key:
            return {"success": False, "reason": "missing_steam_web_api_key", "msg": "Steam Web API key is missing"}
        params = {
            "key": self.api_key,
            "tradeofferid": offer_id,
            "language": "en",
            "get_descriptions": 1,
        }
        try:
            response = self.session.get(STEAM_TRADE_OFFER_URL, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return {"success": False, "reason": "steam_offer_fetch_failed", "msg": str(exc)}
        offer = _extract_offer(payload)
        if not offer:
            return {"success": False, "reason": "trade_offer_not_found", "msg": "trade offer not found", "raw": payload}
        return {"success": True, "offer": offer, "raw": payload}


def _extract_offer(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    offer = response.get("offer") if isinstance(response, dict) else None
    if not isinstance(offer, dict):
        return {}
    descriptions = response.get("descriptions") if isinstance(response.get("descriptions"), list) else []
    by_class_instance = {
        (str(row.get("classid") or ""), str(row.get("instanceid") or "")): row
        for row in descriptions
        if isinstance(row, dict)
    }
    return {
        "tradeofferid": str(offer.get("tradeofferid") or offer.get("trade_offer_id") or ""),
        "trade_offer_state": offer.get("trade_offer_state"),
        "items_to_give": _normalize_items(offer.get("items_to_give"), by_class_instance),
        "items_to_receive": _normalize_items(offer.get("items_to_receive"), by_class_instance),
        "raw": offer,
    }


def _normalize_items(items: Any, descriptions: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        classid = str(item.get("classid") or "")
        instanceid = str(item.get("instanceid") or "")
        desc = descriptions.get((classid, instanceid), {})
        market_hash_name = str(desc.get("market_hash_name") or desc.get("market_name") or item.get("market_hash_name") or "")
        out.append(
            {
                "assetid": str(item.get("assetid") or item.get("asset_id") or ""),
                "classid": classid,
                "instanceid": instanceid,
                "market_hash_name": market_hash_name,
                "amount": item.get("amount"),
            }
        )
    return out
