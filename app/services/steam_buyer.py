from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
import re

import httpx

from app.services.browser_auth import start_login_browser, finish_login_and_extract
from steam.market import list_item
from utils.money import list_price_display_to_cents

logger = logging.getLogger(__name__)

STEAM_COMUNITY_BASE = "https://steamcommunity.com"
STEAM_STORE_BASE = "https://store.steampowered.com"
STEAM_MARKET_BASE = "https://steamcommunity.com/market"


@dataclass
class SteamOrderResult:
    success: bool
    msg: str
    raw: dict[str, Any] | None = None


def build_steam_market_url(market_hash_name: str) -> str:
    return f"{STEAM_MARKET_BASE}/listings/730/{quote(market_hash_name)}"


class SteamBuyer:
    def __init__(self, cookie_str: str = "", session_id: str = "", currency: int = 23):
        self.cookie_str = cookie_str
        self.session_id = session_id
        self.currency = currency

    @staticmethod
    def _parse_cookie_str(cookie_str: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in (cookie_str or "").split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                out[k] = v
        return out

    async def refresh_login(self) -> str:
        await start_login_browser("steam")
        return await finish_login_and_extract("steam")

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": os.getenv("AETHERSWAP_STEAM_UA", "Mozilla/5.0"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": STEAM_MARKET_BASE,
            "Origin": STEAM_COMUNITY_BASE,
        }

    def _build_client(self) -> httpx.Client:
        cookies = self._parse_cookie_str(self.cookie_str)
        cookies.pop("steamCurrencyId", None)
        jar = httpx.Cookies(cookies)
        if self.session_id:
            jar.set("sessionid", self.session_id, domain="steamcommunity.com", path="/")
        jar.set("steamCurrencyId", str(self.currency), domain="steamcommunity.com", path="/")
        return httpx.Client(headers=self._headers(), cookies=jar, timeout=20.0, follow_redirects=True)

    def _build_requests_session(self):
        from steam.session import create_market_session

        return create_market_session(self.cookie_str, "", verify=False)

    def get_wallet_currency(self) -> int:
        return self.currency

    def get_current_highest_buy_order(self, market_hash_name: str) -> float:
        url = f"{STEAM_MARKET_BASE}/priceoverview/?appid=730&currency={self.currency}&market_hash_name={quote(market_hash_name)}"
        with self._build_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        try:
            lowest_price = str(data.get("lowest_price") or "0")
            return float(lowest_price.replace("¥", "").replace("￥", "").replace(",", "").strip() or 0)
        except Exception:
            return 0.0

    def fetch_active_buy_orders(self) -> list[dict[str, Any]]:
        url = f"{STEAM_MARKET_BASE}/mylistings/tradingbuyorders?currency={self.currency}"
        with self._build_client() as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

        orders: list[dict[str, Any]] = []
        for match in re.finditer(r'data-buy-order-id="(?P<order_id>\d+)"[\s\S]{0,2000}?market_hash_name="(?P<name>[^"]+)"[\s\S]{0,2000}?buy_price[^\d]{0,20}(?P<price>[\d\.]+)[\s\S]{0,2000}?quantity[^\d]{0,20}(?P<qty>\d+)', html, re.IGNORECASE):
            try:
                orders.append({
                    "order_id": match.group("order_id"),
                    "market_hash_name": match.group("name"),
                    "my_price": float(match.group("price")),
                    "quantity": int(match.group("qty")),
                })
            except Exception:
                continue
        if not orders:
            for match in re.finditer(r'/market/listings/730/(?P<name>[^"\']+)[\s\S]{0,2000}?Buy Order[^\d]{0,20}(?P<price>[\d\.]+)', html, re.IGNORECASE):
                try:
                    orders.append({
                        "order_id": "",
                        "market_hash_name": match.group("name"),
                        "my_price": float(match.group("price")),
                        "quantity": 1,
                    })
                except Exception:
                    continue
        return orders

    def query_order_status(self, *, order_nums: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        order_ids = {str(x).strip() for x in (order_nums or []) if str(x).strip()}
        try:
            rows = self.fetch_active_buy_orders()
        except Exception as exc:
            return {"success": False, "msg": str(exc), "raw": {"order_nums": list(order_ids)}}

        if order_ids:
            for row in rows:
                if str(row.get("order_id") or "").strip() in order_ids:
                    out = dict(row)
                    out.setdefault("order_status", "open")
                    return {"success": True, "msg": "Steam buy order is still active", "data": out}
            return {
                "success": True,
                "msg": "Steam buy order not found in active orders; keep waiting for settlement evidence",
                "data": [
                    {
                        "order_id": next(iter(order_ids)),
                        "order_status": "pending",
                        "missing_from_active_orders": True,
                    }
                ],
            }

        return {
            "success": True,
            "msg": "Steam active buy orders loaded",
            "data": [dict(row, order_status=row.get("order_status") or "open") for row in rows],
        }

    def create_listing(
        self,
        *,
        assetid: str,
        price: float | None = None,
        price_cents: int | None = None,
        appid: int = 730,
        contextid: str | int = "2",
        amount: int = 1,
        account_currency: str = "CNY",
    ) -> dict[str, Any]:
        assetid = str(assetid or "").strip()
        if not assetid:
            return {"success": False, "msg": "assetid is required", "reason": "validation_error"}
        cents = int(price_cents or 0)
        if cents <= 0:
            if price is None or float(price or 0) <= 0:
                return {"success": False, "msg": "price or price_cents is required", "reason": "validation_error"}
            cents = list_price_display_to_cents(float(price), account_currency=account_currency)
        session_id = self.session_id or self._extract_sessionid()
        if not session_id:
            return {"success": False, "msg": "Steam sessionid is required", "auth_required": True}
        try:
            with self._build_requests_session() as session:
                out = list_item(session, session_id, int(appid), str(contextid or "2"), assetid, cents, amount=int(amount or 1))
        except Exception as exc:
            return {"success": False, "msg": str(exc)}
        if not out:
            return {"success": False, "msg": "Steam listing request returned empty response"}
        text = str(out.get("text") or "")
        try:
            data = out.get("json") if isinstance(out.get("json"), dict) else None
            if data is None:
                import json

                data = json.loads(text or "{}")
        except Exception:
            data = {"text": text[:1000]}
        msg = str(data.get("message") or data.get("msg") or "")
        msg_lower = msg.lower()
        success = bool(data.get("success")) or "pending confirmation" in msg_lower or "already have a listing" in msg_lower
        return {
            "success": success,
            "msg": msg or ("Steam listing submitted" if success else "Steam listing failed"),
            "assetid": assetid,
            "price_cents": cents,
            "listing_id": data.get("listingid") or data.get("listing_id"),
            "raw": {"status_code": out.get("status_code"), "response": data, "text": text[:1000]},
        }

    def cancel_buy_order(self, order_id: str) -> SteamOrderResult:
        if not str(order_id).strip():
            return SteamOrderResult(False, "order_id 不能为空")
        url = f"{STEAM_MARKET_BASE}/cancelbuyorder/"
        payload = {
            "sessionid": self.session_id or self._extract_sessionid(),
            "buy_orderid": str(order_id),
        }
        with self._build_client() as client:
            resp = client.post(url, data=payload)
            text = resp.text
            if resp.status_code >= 400:
                return SteamOrderResult(False, f"Steam 返回 {resp.status_code}", {"text": text[:500]})
            try:
                data = resp.json()
            except Exception:
                data = {"text": text[:1000]}
            if isinstance(data, dict) and data.get("success") in (1, True, "true"):
                return SteamOrderResult(True, "Steam 求购单已撤销", data)
            return SteamOrderResult(False, str(data.get("message") or data.get("msg") or "Steam 撤单失败"), data)

    def create_buy_order(self, market_hash_name: str, price: float, quantity: int = 1) -> SteamOrderResult:
        if price <= 0:
            return SteamOrderResult(False, "price 必须大于 0")
        if quantity < 1:
            return SteamOrderResult(False, "quantity 必须大于 0")

        # Steam 市场真实求购接口，使用已登录 session 提交
        # 具体请求参数可能因地区/币种略有差异，这里采用市场通用提交方式
        url = f"{STEAM_MARKET_BASE}/createbuyorder/"
        payload = {
            "sessionid": self.session_id or self._extract_sessionid(),
            "appid": 730,
            "market_hash_name": market_hash_name,
            "price_total": int(round(price * quantity * 100)),
            "quantity": quantity,
            "currency": self.get_wallet_currency(),
        }
        with self._build_client() as client:
            resp = client.post(url, data=payload)
            text = resp.text
            try:
                data = resp.json()
            except Exception:
                data = {"text": text[:1000]}
            success_flag = data.get("success") if isinstance(data, dict) else None
            need_confirmation = False
            confirmation_id = ""
            order_id = ""
            if isinstance(data, dict):
                need_confirmation = bool(
                    data.get("need_confirmation")
                    or data.get("needs_confirmation")
                    or isinstance(data.get("confirmation"), dict)
                )
                confirmation = data.get("confirmation") if isinstance(data.get("confirmation"), dict) else {}
                confirmation_id = str(
                    confirmation.get("confirmation_id")
                    or confirmation.get("id")
                    or data.get("confirmation_id")
                    or ""
                ).strip()
                order_id = str(
                    data.get("buy_orderid")
                    or data.get("buy_order_id")
                    or data.get("order_id")
                    or data.get("orderid")
                    or ""
                ).strip()
            if success_flag in (1, True, "true", 22, "22") or need_confirmation:
                out = dict(data) if isinstance(data, dict) else {"raw": data}
                if not order_id and confirmation_id:
                    # Steam may return 406 + success=22 when order creation needs user confirmation.
                    # Keep a stable external id so the worker can continue tracking this action.
                    order_id = confirmation_id
                if order_id:
                    out.setdefault("order_id", order_id)
                if confirmation_id:
                    out["confirmation_id"] = confirmation_id
                if need_confirmation:
                    out["need_confirmation"] = True
                out.setdefault("order_status", "pending")
                msg = "Steam 求购单已提交，待确认" if need_confirmation else "Steam 求购单已提交"
                return SteamOrderResult(True, msg, out)
            if resp.status_code >= 400:
                detail = ""
                if isinstance(data, dict):
                    detail = str(data.get("message") or data.get("msg") or "").strip()
                return SteamOrderResult(False, detail or f"Steam 返回 {resp.status_code}", data if isinstance(data, dict) else {"text": text[:500]})
            return SteamOrderResult(False, str(data.get("message") or data.get("msg") or "Steam 求购失败"), data)

    def _extract_sessionid(self) -> str:
        if self.session_id:
            return self.session_id
        cookies = self._parse_cookie_str(self.cookie_str)
        return cookies.get("sessionid", "")
