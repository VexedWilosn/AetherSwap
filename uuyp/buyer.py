from __future__ import annotations

import base64
import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl

import requests

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

UUYP_API_BASE = "https://api.youpin898.com"
UUYP_BUY_ORDER_URL = f"{UUYP_API_BASE}/api/youpin/bff/trade/purchase/order/savePurchaseOrder"
UUYP_TEMPLATE_ORDER_URL = f"{UUYP_API_BASE}/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderListPC"
UUYP_DIRECT_BUY_URL = f"{UUYP_API_BASE}/api/youpin/bff/trade/order/buy"
UUYP_OFF_SHELF_URL = f"{UUYP_API_BASE}/api/commodity/Commodity/OffShelf"
UUYP_PRICE_CHANGE_URL = f"{UUYP_API_BASE}/api/commodity/Commodity/PriceChangeWithLeaseV2"
BASE_DIR = Path(__file__).resolve().parent.parent
UUYP_HEADERS_PATH = BASE_DIR / "DataEngine" / "uuyp_headers.json"
UUYP_DIRECT_PROXIES = {"http": None, "https": None}


def _parse_cookie_str(cookie_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_str:
        return out
    for key, value in parse_qsl(cookie_str.replace(";", "&"), keep_blank_values=True):
        if key:
            out[key.strip()] = value.strip()
    return out


def _force_uuyp_cny(cookies_dict: dict[str, str]) -> dict[str, str]:
    out = dict(cookies_dict or {})
    out["currency"] = "CNY"
    return out


def _load_extra_headers() -> dict[str, str]:
    try:
        if UUYP_HEADERS_PATH.exists():
            data = json.loads(UUYP_HEADERS_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v is not None and str(v).strip()}
    except Exception:
        pass
    return {}


class UuypBuyer:
    """UUYP purchase API wrapper."""

    def __init__(self, cookie_str: str | dict[str, Any]):
        if isinstance(cookie_str, dict):
            self.raw_credentials = dict(cookie_str)
            self.cookies_dict = _force_uuyp_cny({str(k): str(v) for k, v in cookie_str.items() if v is not None})
        else:
            self.raw_credentials = _parse_cookie_str(cookie_str)
            self.cookies_dict = _force_uuyp_cny(dict(self.raw_credentials))

        # 1. Base request headers
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": "https://www.youpin898.com",
            "Referer": "https://www.youpin898.com/",
            "User-Agent": "okhttp/3.14.9",
            "appType": "1",
            "platform": "android",
            "secret-v": "h5_v1",
            "App-Version": "5.28.3",
            "AppVersion": "5.28.3",
            "AppType": "4",
            "deviceType": "1",
            "package-type": "uuyp",
            "Gameid": "730",
        }
        self.headers.update(_load_extra_headers())
        self.headers["Content-Type"] = "application/json"
        self.headers["Accept"] = "application/json, text/plain, */*"
        self.headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        self.headers["Origin"] = "https://www.youpin898.com"
        self.headers["Referer"] = "https://www.youpin898.com/"

        # 2. Derive Authorization/deviceId from uu_token when available
        uu_token = self.cookies_dict.get("uu_token") or self.cookies_dict.get("UU_TOKEN")
        if uu_token:
            self.headers["Authorization"] = f"Bearer {uu_token}"
            try:
                payload_b64 = uu_token.split(".")[1]
                payload_b64 = payload_b64.replace("-", "+").replace("_", "/")
                payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                payload_data = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
                if payload_data.get("deviceId") and not self.headers.get("deviceId"):
                    self.headers["deviceId"] = str(payload_data["deviceId"])
            except Exception:
                pass

        # 3. Move header-like cookie fields into request headers
        header_keys = ["authorization", "Authorization", "uu_token", "UU_TOKEN", "uk", "deviceId", "deviceid", "deviceUk",
                       "App-Version", "AppVersion", "appType", "platform", "secret-v"]

        for key in header_keys:
            if key in self.cookies_dict:
                val = self.cookies_dict.pop(key)
                if key.lower() == "authorization":
                    if not str(val).lower().startswith("bearer "):
                        val = f"Bearer {val}"
                    self.headers["Authorization"] = val
                elif key in {"uu_token", "UU_TOKEN"}:
                    self.headers["Authorization"] = f"Bearer {val}"
                    self.cookies_dict[key] = val
                elif key.lower() == "deviceid":
                    self.headers["deviceId"] = val
                elif key.lower() in ("app-version", "appversion"):
                    pass
                else:
                    self.headers[key] = val

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(self.headers)
        self.cookies_dict = _force_uuyp_cny(self.cookies_dict)
        self.session.cookies.update(self.cookies_dict)
        self._ensure_risk_headers()

    def _ensure_risk_headers(self) -> None:
        device_id = self.headers.get("deviceId") or self.headers.get("deviceid")
        device_uk = self.headers.get("deviceUk") or self.headers.get("deviceuk")
        if device_id:
            self.headers["deviceId"] = str(device_id)
            self.headers["Device-Id"] = str(device_id)
            self.headers["deviceid"] = str(device_id)
        if device_uk:
            self.headers["deviceUk"] = str(device_uk)
            self.headers["deviceuk"] = str(device_uk)
        self.headers.setdefault("clientType", "1")
        self.headers.setdefault("Client-Type", "1")
        self.headers.setdefault("gameId", "730")
        self.headers.setdefault("Gameid", "730")
        self.headers.setdefault("package-type", "uuyp")
        self.headers.setdefault("deviceType", "1")
        self.headers.setdefault("Sec-Fetch-Dest", "empty")
        self.headers.setdefault("Sec-Fetch-Mode", "cors")
        self.headers.setdefault("Sec-Fetch-Site", "same-site")
        self.session.headers.update(self.headers)

    def _request(self, method: str, url: str, **kwargs) -> dict:
        headers = self.headers.copy()
        headers.update(kwargs.pop("headers", {}) or {})
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        self.cookies_dict = _force_uuyp_cny(self.cookies_dict)
        timeout = kwargs.pop("timeout", 10)
        if "json" in kwargs:
            kwargs.pop("data", None)
            headers.pop("Content-Length", None)
        self._ensure_risk_headers()
        resp = self.session.request(
            method,
            url,
            headers=headers,
            cookies=self.cookies_dict,
            timeout=timeout,
            proxies=UUYP_DIRECT_PROXIES,
            **kwargs,
        )
        try:
            data = resp.json() if resp.text else {}
        except ValueError:
            return {"Code": resp.status_code, "Msg": resp.text[:200], "status_code": resp.status_code}
        if isinstance(data, dict):
            data.setdefault("status_code", resp.status_code)
        return data

    def _refresh_risk_headers(self) -> bool:
        try:
            from DataEngine.uuyp_token_harvester import harvest_uuyp_headers
            fresh = harvest_uuyp_headers()
        except Exception as exc:
            logger.warning("UUYP risk header refresh failed: %s", exc)
            return False
        if not fresh:
            return False
        self.headers.update({str(k): str(v) for k, v in fresh.items() if v})
        self._ensure_risk_headers()
        self.session.headers.update(self.headers)
        return True

    def _normalize_result(self, data: dict) -> tuple[bool, str, Optional[str]]:
        code = data.get("Code", data.get("code"))
        msg = data.get("Msg") or data.get("msg") or data.get("message") or ""
        order_id = None
        payload = data.get("Data") or data.get("data") or {}
        if isinstance(payload, dict):
            order_id = payload.get("OrderNo") or payload.get("orderNo") or payload.get("id") or payload.get("OrderId")
        success = str(code) in {"0", "200"} or code == 0 or data.get("success") is True
        return success, str(msg), str(order_id) if order_id is not None else None

    @staticmethod
    def is_auth_or_risk_error(data: dict | str) -> bool:
        if isinstance(data, dict):
            code = str(data.get("Code", data.get("code", "")))
            msg = str(data.get("Msg") or data.get("msg") or data.get("message") or "")
        else:
            code = ""
            msg = str(data or "")
        text = f"{code} {msg}".lower()
        return code in {"401", "403", "405", "84101", "84104"} or any(
            token in text
            for token in (
                "login",
                "auth",
                "unauthorized",
                "token",
                "risk",
                "verify",
                "\u767b\u5f55",
                "\u767b\u9678",
                "\u98ce\u63a7",
                "\u9a8c\u8bc1",
                "\u4ee4\u724c",
            )
        )

    def create_buy_order(self, goods_id: str | int, price: float, num: int = 1, **kwargs) -> dict:
        template_id = kwargs.get("template_id", goods_id)
        game_id = str(kwargs.get("game_id", kwargs.get("gameId", "730")))
        commodity_name = kwargs.get("commodity_name", kwargs.get("template_name", ""))
        template_hash_name = kwargs.get("template_hash_name", kwargs.get("market_hash_name", commodity_name))
        purchase_price = round(float(price), 2)
        purchase_num = int(num)

        payload = {
            "templateId": int(template_id) if str(template_id).isdigit() else str(template_id),
            "templateHashName": template_hash_name or commodity_name or str(goods_id),
            "commodityName": commodity_name or template_hash_name or str(goods_id),
            "purchasePrice": purchase_price,
            "purchaseNum": purchase_num,
            "needPaymentAmount": round(purchase_price * purchase_num, 2),
            "totalAmount": round(purchase_price * purchase_num, 2),
            "incrementServiceCode": [1001],
            "priceDifference": 0,
            "discountAmount": 0,
            "payConfirmFlag": False,
            "repeatOrderCancelFlag": False,
            "gameId": game_id,
            "gameIdStr": str(game_id),
            "clientType": 1,
        }
        payload["unitPrice"] = purchase_price
        payload["totalPrice"] = round(purchase_price * purchase_num, 2)
        payload["templateName"] = commodity_name or template_hash_name or str(goods_id)
        if kwargs.get("order_no"):
            payload["orderNo"] = kwargs["order_no"]
            payload["templateName"] = commodity_name or template_hash_name or str(goods_id)
            payload["supplyQuantity"] = int(kwargs.get("supply_quantity", 0))
            url = f"{UUYP_API_BASE}/api/youpin/bff/trade/purchase/order/updatePurchaseOrder"
        else:
            url = UUYP_BUY_ORDER_URL

        try:
            res = self._request("POST", url, json=payload)
            if self.is_auth_or_risk_error(res) and self._refresh_risk_headers():
                res = self._request("POST", url, json=payload)
            msg_preview = str(res.get("Msg") or res.get("msg") or "")
            if ("鐧诲綍淇℃伅寮傚父" in msg_preview or "login" in msg_preview.lower()) and self._refresh_risk_headers():
                res = self._request("POST", url, json=payload)
            msg_preview = str(res.get("Msg") or res.get("msg") or "")
            success, msg, order_id = self._normalize_result(res)
            auth_required = (not success) and self.is_auth_or_risk_error(res)
            if auth_required:
                msg = "UUYP ??????????????? uu_token?deviceId/deviceToken?Sessionid/uk ????"
            if not success and "鐧诲綍淇℃伅寮傚父" in msg_preview:
                msg = "UUYP ??????????????? uu_token?deviceId/deviceToken?Sessionid/uk ????"
            if success:
                if not order_id and isinstance(res.get("Data"), dict):
                    order_id = str(res["Data"].get("OrderNo") or res["Data"].get("orderNo") or res["Data"].get("id") or "") or None
                return {"success": True, "msg": msg or "purchase order created", "order_id": order_id}
            return {"success": False, "msg": msg or f"UUYP ??????: {res}", "auth_required": auth_required, "raw": res}
        except Exception as exc:
            logger.exception("UUYP order failed: %s", exc)
            return {"success": False, "msg": str(exc)}

    def get_template_purchase_order_pc(self, template_id: str | int, page_index: int = 1, page_size: int = 30, min_abrade: float = 0, max_abrade: float = 1, type_id: int = -1) -> dict:
        payload = {
            "templateId": str(template_id),
            "pageIndex": page_index,
            "pageSize": page_size,
            "minAbrade": min_abrade,
            "maxAbrade": max_abrade,
            "typeId": type_id,
        }
        return self._request("POST", UUYP_TEMPLATE_ORDER_URL, data=json.dumps(payload, ensure_ascii=False), headers={"platform": "pc"})

    def query_order_status(
        self,
        *,
        order_nums: list[str] | None = None,
        template_id: str | int | None = None,
        game_id: str = "730",
        page_size: int = 30,
        **kwargs: Any,
    ) -> dict[str, Any]:
        order_ids = {str(x).strip() for x in (order_nums or []) if str(x).strip()}
        template_id = template_id or kwargs.get("templateId") or kwargs.get("goods_id")
        if not template_id:
            return {
                "success": True,
                "msg": "UUYP order query requires template_id; keep waiting",
                "data": [
                    {
                        "OrderNo": next(iter(order_ids), ""),
                        "orderStatus": "pending",
                        "missing_template_id": True,
                    }
                ],
            }
        res = self.get_template_purchase_order_pc(
            template_id=template_id,
            page_index=1,
            page_size=max(1, min(int(page_size or 30), 100)),
        )
        success, msg, _ = self._normalize_result(res)
        auth_required = (not success) and self.is_auth_or_risk_error(res)
        if auth_required:
            return {"success": False, "msg": msg or "UUYP auth/risk error", "auth_required": True, "raw": res}

        payload = res.get("Data") or res.get("data") or {}
        rows = []
        if isinstance(payload, dict):
            for key in ("list", "List", "items", "Items", "records", "Records", "rows", "Rows"):
                val = payload.get(key)
                if isinstance(val, list):
                    rows = [row for row in val if isinstance(row, dict)]
                    break
        elif isinstance(payload, list):
            rows = [row for row in payload if isinstance(row, dict)]

        if order_ids:
            for row in rows:
                if any(str(row.get(key) or "").strip() in order_ids for key in ("OrderNo", "orderNo", "OrderId", "orderId", "id")):
                    matched = dict(row)
                    matched.setdefault("orderStatus", matched.get("orderStatus") or matched.get("status") or "pending")
                    return {"success": True, "msg": "UUYP order status loaded", "data": matched, "raw": res}
            return {
                "success": True,
                "msg": "UUYP order not found in template order page; keep waiting",
                "data": [{"OrderNo": next(iter(order_ids)), "orderStatus": "pending", "not_found_in_template_orders": True}],
                "raw": res,
            }
        return {"success": True, "msg": "UUYP order status loaded", "data": rows, "raw": res}

    def buy_listing(self, commodity_no: str | int, price: float, **kwargs) -> dict:
        payload = {
            "commodityNo": str(commodity_no),
            "gameId": str(kwargs.get("game_id", kwargs.get("gameId", "730"))),
            "price": round(float(price), 2),
        }
        try:
            res = self._request("POST", UUYP_DIRECT_BUY_URL, json=payload)
            success, msg, order_id = self._normalize_result(res)
            return {
                "success": success,
                "msg": msg or ("buy success" if success else f"UUYP buy api error: {res}"),
                "order_id": order_id,
                "request_payload": payload,
                "raw": res,
            }
        except Exception as exc:
            logger.exception("UUYP璐拱澶辫触: %s", exc)
            return {"success": False, "msg": str(exc), "request_payload": payload}

    def off_shelf(self, commodity_ids: list[str] | tuple[str, ...] | str) -> dict[str, Any]:
        ids = [str(commodity_ids).strip()] if isinstance(commodity_ids, (str, int)) else [str(x).strip() for x in commodity_ids if str(x).strip()]
        if not ids:
            return {"success": False, "msg": "commodity_id is required", "reason": "validation_error"}
        payload = {
            "Ids": ",".join(ids),
            "IsDeleteCommodityCache": 1,
            "IsForceOffline": True,
        }
        try:
            res = self._request("PUT", UUYP_OFF_SHELF_URL, json=payload)
            success, msg, _ = self._normalize_result(res)
            auth_required = (not success) and self.is_auth_or_risk_error(res)
            return {
                "success": success,
                "msg": msg or ("UUYP commodity off-shelf submitted" if success else "UUYP off-shelf failed"),
                "platform_listing_id": ids[0] if len(ids) == 1 else None,
                "data": {"cancelled": ids, "order_status": "cancelled" if success else "pending"},
                "auth_required": auth_required,
                "request_payload": payload,
                "raw": res,
            }
        except Exception as exc:
            return {"success": False, "msg": str(exc), "request_payload": payload}

    def change_price(self, assets: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
        if isinstance(assets, dict):
            item_infos = [
                {"CommodityId": int(k) if str(k).isdigit() else str(k), "Price": str(v), "Remark": None, "IsCanSold": True}
                for k, v in assets.items()
                if str(k).strip() and v not in (None, "")
            ]
        else:
            item_infos = []
            for row in assets:
                if not isinstance(row, dict):
                    continue
                commodity_id = row.get("CommodityId") or row.get("commodity_id") or row.get("commodityId") or row.get("platform_listing_id")
                price = row.get("Price") or row.get("price")
                if commodity_id in (None, "") or price in (None, ""):
                    continue
                item_infos.append(
                    {
                        "CommodityId": int(commodity_id) if str(commodity_id).isdigit() else str(commodity_id),
                        "Price": str(price),
                        "Remark": row.get("Remark"),
                        "IsCanSold": bool(row.get("IsCanSold", True)),
                    }
                )
        if not item_infos:
            return {"success": False, "msg": "CommodityId and Price are required", "reason": "validation_error"}
        payload = {"Commoditys": item_infos}
        try:
            res = self._request("PUT", UUYP_PRICE_CHANGE_URL, json=payload)
            success, msg, _ = self._normalize_result(res)
            auth_required = (not success) and self.is_auth_or_risk_error(res)
            return {
                "success": success,
                "msg": msg or ("UUYP price change submitted" if success else "UUYP price change failed"),
                "data": {
                    "changed": [str(row["CommodityId"]) for row in item_infos],
                    "order_status": "reprice_submitted" if success else "pending",
                },
                "auth_required": auth_required,
                "request_payload": payload,
                "raw": res,
            }
        except Exception as exc:
            return {"success": False, "msg": str(exc), "request_payload": payload}

    def buy(self, *args: Any, **kwargs: Any) -> Any:
        return self.create_buy_order(*args, **kwargs)

    def sell(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def query_inventory(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def search_item_id_by_name(self, market_hash_name: str) -> Optional[str]:
        """Search UUYP templateId by item name."""
        try:
            payload = {
                "keyword": market_hash_name,
                "pageIndex": 1,
                "pageSize": 10,
                "sortType": 0,
                "gameId": "730"
            }
            res = self._request("POST", f"{UUYP_API_BASE}/api/youpin/bff/new/commodity/v2/search/list", data=json.dumps(payload))
            
            data = res.get("data", {})
            if isinstance(data, dict):
                commodity_list = data.get("commodityList", [])
                if commodity_list:
                    # Prefer exact name matches
                    for item in commodity_list:
                        if item.get("commodityName", "").lower() == market_hash_name.lower():
                            return str(item.get("templateId"))
                    # Fallback to the first returned template id
                    return str(commodity_list[0].get("templateId"))
            return None
        except Exception as exc:
            logger.warning("UUYP template search failed: %s", exc)
            return None
