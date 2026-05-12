from __future__ import annotations

import json
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_BASE = "https://api.youpin898.com"
WEB_BASE = "https://www.youpin898.com"
DEFAULT_TIMEOUT = 15
LOCAL_TEMPLATE_MAP = {
    "p250 | copper oxide (field-tested)": "110797",
    "nova | wood fired (battle-scarred)": "1397",
    "mp9 | orange peel (minimal wear)": "5472",
}


def _random_uk(length: int = 65) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def _fetch_uuyp_uk(headers: Optional[dict] = None) -> str:
    current = (headers or {}).get("uk") if isinstance(headers, dict) else ""
    return str(current or _random_uk())


def _load_saved_headers() -> dict:
    path = Path(__file__).resolve().parent.parent / "DataEngine" / "uuyp_headers.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_credentials() -> dict:
    try:
        from config import get

        data = get("uuyp", default={})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _cookie_dict(cookie_str: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie_str or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def _cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if value is not None)


def _first_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("Data", "data", "list", "rows", "items", "purchaseOrderResponseList"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _first_list(value)
            if nested:
                return nested
    return []


class UuypBuyer:
    """Small UUYP client for market lookup and purchase-order automation.

    Direct web purchase is intentionally not implemented here because UUYP's PC
    site currently sends users through an app-only trade path. The reliable
    automation surface is the purchase-order endpoint mirrored by Steamauto.
    """

    def __init__(
        self,
        cookies: str | None = None,
        cookie_str: str | dict | None = None,
        headers: Optional[dict] = None,
        session: Optional[requests.Session] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        creds = _load_credentials()
        saved_headers = _load_saved_headers()
        raw_cookie_input = cookie_str if cookie_str is not None else cookies
        self.cookies_dict: dict[str, str] = {}
        promoted_headers: dict[str, str] = {}
        if isinstance(raw_cookie_input, dict):
            for key, value in raw_cookie_input.items():
                if value is None:
                    continue
                key_str = str(key)
                val = str(value)
                low = key_str.lower()
                if low == "authorization":
                    promoted_headers["Authorization"] = val if val.lower().startswith("bearer ") else f"Bearer {val}"
                elif low == "deviceid":
                    promoted_headers["deviceId"] = val
                elif key_str in {"DeviceToken", "Sessionid", "uk", "deviceUk", "DeviceId"}:
                    promoted_headers[key_str] = val
                else:
                    self.cookies_dict[key_str] = val
            self.cookies = _cookie_string(self.cookies_dict)
        else:
            self.cookies = raw_cookie_input if raw_cookie_input is not None else str(creds.get("cookies") or "")
            self.cookies_dict = _cookie_dict(self.cookies)
        self.headers = {**saved_headers, **promoted_headers, **(headers or {})}
        for key in ("DeviceToken", "Sessionid", "deviceToken", "sessionid", "token", "uu_token"):
            value = creds.get(key)
            if value and key not in self.headers:
                self.headers[key] = str(value)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = False
        if self.cookies_dict:
            self.session.cookies.update(self.cookies_dict)
        self._uk = str(self.headers.get("uk") or "")
        self._uk_time = 0.0
        self.last_select_listing_error: Optional[dict] = None

    @staticmethod
    def market_url(template_id: str | int, game_id: str | int = 730, list_type: str | int = 10) -> str:
        return f"{WEB_BASE}/market/goods-list?listType={list_type}&templateId={template_id}&gameId={game_id}"

    def _request_headers(self, uk_verify: bool = False, pc_platform: bool = False) -> dict:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Origin": WEB_BASE,
            "Referer": f"{WEB_BASE}/",
            "User-Agent": self.headers.get(
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ),
            "App-Version": str(self.headers.get("App-Version") or self.headers.get("AppVersion") or "5.26.0"),
            "AppVersion": str(self.headers.get("AppVersion") or self.headers.get("App-Version") or "5.26.0"),
            "secret-v": str(self.headers.get("secret-v") or "h5_v1"),
            "appType": str(self.headers.get("appType") or "1"),
            "platform": "pc" if pc_platform else str(self.headers.get("platform") or "android"),
        }
        for key in (
            "authorization",
            "Authorization",
            "DeviceToken",
            "DeviceId",
            "deviceId",
            "deviceUk",
            "Gameid",
            "Sessionid",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
        ):
            value = self.headers.get(key)
            if value:
                headers[key] = str(value)
        if self.cookies:
            headers["Cookie"] = self.cookies
        if uk_verify:
            headers["uk"] = self._get_uk()
        return headers

    def _get_uk(self) -> str:
        if self._uk and time.time() - self._uk_time < 30:
            return self._uk
        self._uk = _fetch_uuyp_uk(self.headers)
        self._uk_time = time.time()
        return self._uk

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
        uk_verify: bool = False,
        pc_platform: bool = False,
        **kwargs: Any,
    ) -> dict:
        request_headers = self._request_headers(uk_verify=False, pc_platform=pc_platform)
        if headers:
            request_headers.update(headers)
        if uk_verify:
            request_headers["uk"] = _fetch_uuyp_uk(request_headers)
        response = self.session.request(
            method.upper(),
            url,
            json=json,
            headers=request_headers,
            timeout=kwargs.pop("timeout", self.timeout),
            verify=False,
            **kwargs,
        )
        try:
            data = response.json()
        except Exception:
            data = {"text": getattr(response, "text", ""), "status_code": getattr(response, "status_code", None)}
        if isinstance(data, dict):
            data.setdefault("status_code", getattr(response, "status_code", None))
        return data

    def _response(self, data: dict):
        class JsonResponse:
            def __init__(self, payload: dict):
                self._payload = payload
                self.status_code = int(payload.get("status_code") or 200)
                self.text = json_module.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(self.text)

        json_module = json
        return JsonResponse(data)

    def call_api(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        *,
        uk_verify: bool = False,
        pc_platform: bool = False,
    ) -> requests.Response:
        url = path if path.startswith("http") else API_BASE + path
        payload = self._request(method, url, json=data or {}, uk_verify=uk_verify, pc_platform=pc_platform)
        return self._response(payload)

    def query_sale_template(self, key_words: str = "", page_index: int = 1, page_size: int = 20) -> dict:
        payload: dict[str, Any] = {
            "listSortType": 0,
            "sortType": 0,
            "pageSize": int(page_size),
            "pageIndex": int(page_index),
        }
        if key_words:
            payload["keyWords"] = key_words
        return self._request(
            "POST",
            API_BASE + "/api/homepage/pc/goods/market/querySaleTemplate",
            json=payload,
            uk_verify=True,
            pc_platform=True,
        )

    def search_item_id_by_name(self, market_hash_name: str) -> Optional[str]:
        name = (market_hash_name or "").strip()
        if not name:
            return None
        local = LOCAL_TEMPLATE_MAP.get(" ".join(name.lower().split()))
        if local:
            return local
        data = self.query_sale_template(name)
        for item in _first_list(data):
            hash_name = str(item.get("commodityHashName") or item.get("templateHashName") or "").strip()
            template_id = item.get("id") or item.get("templateId")
            if template_id and hash_name == name:
                return str(template_id)
        for item in _first_list(data):
            template_id = item.get("id") or item.get("templateId")
            if template_id:
                return str(template_id)
        return None

    def query_on_sale_commodity_list(
        self,
        template_id: str | int,
        page_index: int = 1,
        page_size: int = 10,
        *,
        max_price: float | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "gameId": "730",
            "listType": "10",
            "templateId": str(template_id),
            "listSortType": 1,
            "sortType": 0,
            "pageIndex": int(page_index),
            "pageSize": int(page_size),
        }
        if max_price is not None and float(max_price) > 0:
            payload["maxPrice"] = round(float(max_price), 2)
            payload["maxSaleVal"] = round(float(max_price), 2)
        headers = self._request_headers(uk_verify=True, pc_platform=True)
        headers["Referer"] = self.market_url(template_id)
        return self._request(
            "POST",
            API_BASE + "/api/homepage/pc/goods/market/queryOnSaleCommodityList",
            json=payload,
            headers=headers,
            uk_verify=True,
            pc_platform=True,
        )

    def select_best_listing(self, template_id: str | int, max_price: float | None = None) -> Optional[dict]:
        self.last_select_listing_error = None
        data = self.query_on_sale_commodity_list(template_id, page_size=20, max_price=max_price)
        code = data.get("Code", data.get("code", 0)) if isinstance(data, dict) else 0
        if code not in (0, None):
            self.last_select_listing_error = {
                "code": code,
                "msg": data.get("Msg") or data.get("msg"),
                "status_code": data.get("status_code"),
            }
            return None
        best: Optional[dict] = None
        for item in _first_list(data):
            try:
                price = float(item.get("price"))
            except Exception:
                continue
            if max_price is not None and price > float(max_price):
                continue
            if best is None or price < float(best.get("price", 0) or 0):
                best = dict(item)
                best["price"] = price
                best["_selected_commodity_no"] = str(item.get("commodityNo") or item.get("id") or "")
                best["_selected_price"] = price
        return best

    def publish_purchase_order(
        self,
        template_id: str | int,
        template_hash_name: str,
        commodity_name: str,
        purchase_price: float,
        purchase_num: int,
        *,
        order_no: str = "",
        supply_quantity: int = 0,
    ) -> requests.Response:
        price = round(float(purchase_price), 2)
        num = max(1, int(purchase_num))
        payload: dict[str, Any] = {
            "templateId": int(template_id) if str(template_id).isdigit() else str(template_id),
            "templateHashName": template_hash_name,
            "commodityName": commodity_name,
            "purchasePrice": price,
            "purchaseNum": num,
            "needPaymentAmount": round(num * price, 2),
            "totalAmount": round(num * price, 2),
            "incrementServiceCode": [1001],
            "priceDifference": 0,
            "discountAmount": 0,
            "payConfirmFlag": False,
            "repeatOrderCancelFlag": False,
        }
        path = "/api/youpin/bff/trade/purchase/order/savePurchaseOrder"
        if order_no:
            payload["orderNo"] = order_no
            payload["templateName"] = commodity_name
            payload["supplyQuantity"] = supply_quantity
            path = "/api/youpin/bff/trade/purchase/order/updatePurchaseOrder"
        data = self._request("POST", API_BASE + path, json=payload, uk_verify=True, pc_platform=True)
        return self._response(data)

    def create_buy_order(
        self,
        template_id: str | int,
        price: float,
        quantity: int = 1,
        market_hash_name: str = "",
        commodity_name: str = "",
    ) -> dict:
        response = self.publish_purchase_order(
            template_id,
            market_hash_name or commodity_name,
            commodity_name or market_hash_name,
            price,
            quantity,
        )
        try:
            return response.json()
        except Exception:
            return {"ok": False, "error": response.text}

    def get_template_purchase_order_pc(
        self,
        template_id: str | int,
        page_index: int = 1,
        page_size: int = 30,
        min_abrade: float = 0,
        max_abrade: float = 1,
        type_id: int = -1,
    ) -> dict:
        return self._request(
            "POST",
            API_BASE + "/api/youpin/bff/trade/purchase/order/getTemplatePurchaseOrderListPC",
            json={
                "templateId": str(template_id),
                "pageIndex": page_index,
                "pageSize": page_size,
                "minAbrade": min_abrade,
                "maxAbrade": max_abrade,
                "typeId": type_id,
            },
            headers=self._request_headers(uk_verify=True, pc_platform=True),
            uk_verify=True,
            pc_platform=True,
        )

    def search_purchase_order_list(self, page_index: int = 1, page_size: int = 40, status: int = 20) -> dict:
        return self._request(
            "POST",
            API_BASE + "/api/youpin/bff/trade/purchase/order/searchPurchaseOrderList",
            json={"pageIndex": page_index, "pageSize": page_size, "status": status},
            uk_verify=True,
            pc_platform=True,
        )

    def query_order_status(self, order_nums: list[str], template_id: str | int | None = None) -> dict:
        order_set = {str(order) for order in (order_nums or []) if str(order or "").strip()}
        if not order_set:
            return {"success": False, "msg": "order_nums is required"}
        if not template_id:
            return {
                "success": True,
                "data": [{"OrderNo": order, "orderStatus": "pending", "missing_template_id": True} for order in order_set],
            }
        data = self.get_template_purchase_order_pc(template_id=template_id, page_index=1, page_size=30)
        code = data.get("Code", data.get("code", 0)) if isinstance(data, dict) else 0
        if code not in (0, None):
            msg = str(data.get("Msg") or data.get("msg") or "")
            auth_required = code in {401, 84101, 84103, 2002} or any(
                token in msg.lower() for token in ("login", "auth", "token", "risk", "登录", "风控")
            )
            return {"success": False, "msg": msg, "auth_required": auth_required, "raw": data}
        for item in _first_list(data):
            order_no = str(item.get("OrderNo") or item.get("orderNo") or item.get("purchaseNo") or "")
            if order_no in order_set:
                return {"success": True, "data": item}
        return {
            "success": True,
            "data": [{"OrderNo": order, "orderStatus": "pending", "not_found": True} for order in order_set],
        }

    def change_price(self, commodity_price_map: dict[str, float]) -> dict:
        items = [
            {"CommodityId": int(commodity_id), "Price": f"{float(price):.2f}"}
            for commodity_id, price in (commodity_price_map or {}).items()
        ]
        payload: dict[str, Any] = {"Commoditys": items}
        sessionid = self.headers.get("Sessionid") or self.headers.get("DeviceToken")
        if sessionid:
            payload["Sessionid"] = sessionid
        data = self._request(
            "PUT",
            API_BASE + "/api/youpin/bff/new/commodity/commodity/change/price/v3/confirm",
            json=payload,
            uk_verify=True,
            pc_platform=True,
        )
        ok = data.get("Code", data.get("code", 0)) == 0
        changed = []
        for item in _first_list(data):
            if item.get("IsSuccess") in (1, True) or item.get("success") is True:
                changed.append(str(item.get("CommodityId") or item.get("commodityId") or items[0]["CommodityId"]))
        if ok and not changed:
            changed = [str(item["CommodityId"]) for item in items]
        return {
            "success": bool(ok),
            "msg": data.get("Msg") or data.get("msg") or "",
            "data": {"changed": changed, "order_status": "reprice_submitted"},
            "raw": data,
        }

    def off_shelf(self, commodity_ids: list[str]) -> dict:
        ids = [str(item) for item in commodity_ids if str(item or "").strip()]
        payload: dict[str, Any] = {"Ids": ",".join(ids)}
        sessionid = self.headers.get("Sessionid") or self.headers.get("DeviceToken")
        if sessionid:
            payload["Sessionid"] = sessionid
        data = self._request(
            "PUT",
            API_BASE + "/api/youpin/bff/new/commodity/commodity/off/shelf",
            json=payload,
            uk_verify=True,
            pc_platform=True,
        )
        ok = data.get("Code", data.get("code", 0)) == 0
        return {
            "success": bool(ok),
            "msg": data.get("Msg") or data.get("msg") or "",
            "data": {"cancelled": ids, "order_status": "cancelled"},
            "raw": data,
        }

    def buy_listing(self, commodity_no: str, price: float) -> dict:
        data = self._request(
            "POST",
            API_BASE + "/api/youpin/bff/trade/order/buy",
            json={"commodityNo": str(commodity_no), "price": round(float(price), 2)},
            uk_verify=True,
            pc_platform=True,
        )
        code = data.get("Code", data.get("code"))
        if code == 84004 or "not found" in str(data.get("Msg") or data.get("msg") or "").lower():
            return {
                "success": False,
                "reason": "direct_buy_unsupported",
                "msg": "UUYP direct buy is not available on the PC web path; use purchase_order or manual confirmation.",
                "raw": data,
            }
        ok = code == 0
        return {"success": bool(ok), "msg": data.get("Msg") or data.get("msg") or "", "raw": data}
