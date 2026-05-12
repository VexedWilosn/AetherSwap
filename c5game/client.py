from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

C5GAME_OPENAPI_BASE_URL = "http://openapi.c5game.com"


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("list", "List", "items", "Items", "rows", "Rows", "records", "Records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _order_id(row: dict[str, Any]) -> str:
    return str(_first_value(row, "orderId", "order_id", "OrderId", "id") or "").strip()


def _status_from_c5(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "0": "pending_payment",
        "1": "wait_send",
        "2": "delivering",
        "3": "waiting_receive",
        "10": "completed",
        "11": "cancelled",
    }
    return mapping.get(text, text or "unknown")


class C5GameClient:
    """Small C5Game OpenAPI wrapper for seller delivery flows.

    The public Steamauto reference only covers account balance, merchant order
    polling, and requesting C5Game to send a Steam offer. Keep this client narrow
    until verified buy/listing APIs are available locally.
    """

    def __init__(
        self,
        app_key: str,
        *,
        base_url: str = C5GAME_OPENAPI_BASE_URL,
        timeout: int = 15,
        session: requests.Session | None = None,
    ):
        self.app_key = str(app_key or "").strip()
        if not self.app_key:
            raise ValueError("C5Game app_key is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = max(1, int(timeout or 15))
        self.session = session or requests.Session()
        self.session.headers.update({"app-key": self.app_key})

    @staticmethod
    def is_success(data: dict[str, Any]) -> bool:
        if not isinstance(data, dict):
            return False
        if bool(data.get("success")):
            return True
        code = str(data.get("code") or data.get("errorCode") or data.get("status") or "").strip().lower()
        return code in {"0", "200", "ok", "success"}

    @staticmethod
    def result_message(data: dict[str, Any]) -> str:
        return str(data.get("message") or data.get("msg") or data.get("errorMsg") or data.get("error") or "")

    def _normalize_response(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        return {"success": False, "message": f"Invalid C5Game response type: {type(data).__name__}", "data": data}

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params or {}, timeout=self.timeout)
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {"success": False, "message": resp.text[:500], "status_code": resp.status_code}
        data = self._normalize_response(data)
        if resp.status_code != 200 and "status_code" not in data:
            data["status_code"] = resp.status_code
        return data

    def post(self, path: str, payload: Any) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {"success": False, "message": resp.text[:500], "status_code": resp.status_code}
        data = self._normalize_response(data)
        if resp.status_code != 200 and "status_code" not in data:
            data["status_code"] = resp.status_code
        return data

    def balance(self) -> dict[str, Any]:
        data = self.get("/merchant/account/v1/balance")
        return self._wrap(data, fallback_msg="C5Game balance loaded")

    def order_list(self, *, status: int | None = None, page: int = 1, steam_id: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"page": max(1, int(page or 1))}
        if status is not None:
            params["status"] = int(status)
        if steam_id:
            params["steamId"] = str(steam_id)
        data = self.get("/merchant/order/v1/list", params)
        if not self.is_success(data):
            return self._wrap(data, fallback_msg="C5Game order list failed")
        rows = _rows_from_payload(data.get("data") or data.get("Data") or data)
        return {
            "success": True,
            "msg": self.result_message(data) or "C5Game order list loaded",
            "data": rows,
            "raw": data,
            "request_payload": params,
        }

    def query_order_status(
        self,
        *,
        order_nums: list[str] | None = None,
        status: int | None = None,
        steam_id: str = "",
        page: int = 1,
    ) -> dict[str, Any]:
        result = self.order_list(status=status, page=page, steam_id=steam_id)
        if not result.get("success"):
            return result
        rows = [dict(row) for row in result.get("data") or [] if isinstance(row, dict)]
        targets = {str(order_id).strip() for order_id in (order_nums or []) if str(order_id).strip()}
        if targets:
            for row in rows:
                if _order_id(row) in targets:
                    row.setdefault("order_status", _status_from_c5(_first_value(row, "status", "orderStatus", "order_status")))
                    return {"success": True, "msg": "C5Game order status loaded", "data": row, "raw": result.get("raw")}
            return {
                "success": True,
                "msg": "C5Game order not found in order list; keep waiting",
                "data": {
                    "order_id": next(iter(targets)),
                    "order_status": "pending",
                    "not_found_in_order_list": True,
                },
                "raw": result.get("raw"),
            }
        for row in rows:
            row.setdefault("order_status", _status_from_c5(_first_value(row, "status", "orderStatus", "order_status")))
        return {"success": True, "msg": "C5Game order status loaded", "data": rows, "raw": result.get("raw")}

    def deliver(self, order_ids: list[str | int] | tuple[str | int, ...] | str | int) -> dict[str, Any]:
        ids = [str(order_ids).strip()] if isinstance(order_ids, (str, int)) else [str(x).strip() for x in order_ids if str(x).strip()]
        if not ids:
            return {"success": False, "msg": "C5Game order_id is required", "reason": "validation_error"}
        payload = [int(order_id) if order_id.isdigit() else order_id for order_id in ids]
        data = self.post("/merchant/order/v1/deliver", payload)
        if self.is_success(data):
            return {
                "success": True,
                "msg": self.result_message(data) or "C5Game deliver request submitted",
                "platform_order_id": ids[0] if len(ids) == 1 else None,
                "data": {"order_ids": ids, "order_status": "delivering"},
                "request_payload": payload,
                "raw": data,
            }
        result = self._wrap(data, fallback_msg="C5Game deliver request failed")
        result["request_payload"] = payload
        return result

    def find_trade_offer_id(
        self,
        *,
        order_id: str | int,
        steam_id: str = "",
        page: int = 1,
    ) -> dict[str, Any]:
        order_id_str = str(order_id or "").strip()
        if not order_id_str:
            return {"success": False, "msg": "C5Game order_id is required", "reason": "validation_error"}
        result = self.query_order_status(order_nums=[order_id_str], status=2, steam_id=steam_id, page=page)
        if not result.get("success"):
            return result
        row = result.get("data") if isinstance(result.get("data"), dict) else {}
        info = row.get("orderConfirmInfoDTO") if isinstance(row, dict) else {}
        offer_id = ""
        if isinstance(info, dict):
            offer_id = str(_first_value(info, "offerId", "offer_id", "tradeOfferId", "trade_offer_id") or "").strip()
        if not offer_id and isinstance(row, dict):
            offer_id = str(_first_value(row, "offerId", "offer_id", "tradeOfferId", "trade_offer_id") or "").strip()
        if offer_id:
            return {
                "success": True,
                "msg": "C5Game trade offer found",
                "platform_order_id": order_id_str,
                "trade_offer_id": offer_id,
                "data": {
                    "order_id": order_id_str,
                    "trade_offer_id": offer_id,
                    "order_status": "pending",
                },
                "raw": result.get("raw"),
            }
        return {
            "success": True,
            "msg": "C5Game trade offer is not ready yet",
            "platform_order_id": order_id_str,
            "data": {
                "order_id": order_id_str,
                "order_status": "pending",
                "trade_offer_pending": True,
            },
            "raw": result.get("raw"),
        }

    def _wrap(self, data: dict[str, Any], *, fallback_msg: str) -> dict[str, Any]:
        success = self.is_success(data)
        result = {
            "success": success,
            "msg": self.result_message(data) or fallback_msg,
            "data": data.get("data") if isinstance(data, dict) else None,
            "raw": data,
        }
        text = f"{data.get('errorCode', '')} {result['msg']}".lower() if isinstance(data, dict) else result["msg"].lower()
        if not success and any(token in text for token in ("app_key", "app-key", "auth", "token", "unauthorized", "400001")):
            result["auth_required"] = True
        return result
