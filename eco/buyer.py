from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .openapi_client import EcoOpenAPIClient, EcoOpenAPIError

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _result_payload(data: dict[str, Any]) -> Any:
    return data.get("ResultData") or data.get("resultData") or data.get("data")


def _rows_from_page_result(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("PageResult", "pageResult", "List", "list", "Rows", "rows", "Items", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _operation_rows(data: Any) -> list[dict[str, Any]]:
    payload = _result_payload(data) if isinstance(data, dict) else data
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _row_success(row: dict[str, Any]) -> bool:
    value = row.get("IsSuccess", row.get("isSuccess", row.get("success", row.get("Success"))))
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() in {"1", "true", "ok", "success", "succeeded"}


def _first_row_value(rows: list[dict[str, Any]], *keys: str) -> Any:
    for row in rows:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value
    return None


def _make_merchant_no(prefix: str = "AS") -> str:
    return f"{prefix}{int(time.time() * 1000)}"


class EcoBuyer:
    """ECO Steam buyer backed by official RSA OpenAPI."""

    def __init__(self, cookie_str: str | dict[str, Any] | None = None, *, client: EcoOpenAPIClient | None = None):
        credentials = cookie_str if isinstance(cookie_str, dict) else None
        self.client = client or EcoOpenAPIClient(credentials=credentials)

    def _ok_result(self, data: dict[str, Any], *, fallback_msg: str = "success") -> dict[str, Any]:
        payload = _result_payload(data)
        return {
            "success": True,
            "msg": self.client.result_message(data) or fallback_msg,
            "code": str(data.get("ResultCode", "")),
            "data": payload,
            "raw": data,
        }

    def _fail_result(self, data: dict[str, Any], *, fallback_msg: str = "ECO OpenAPI failed") -> dict[str, Any]:
        code = str(data.get("ResultCode", data.get("code", "")))
        msg = self.client.result_message(data) or fallback_msg
        result: dict[str, Any] = {"success": False, "msg": msg, "code": code, "raw": data}
        payload = _result_payload(data)
        if isinstance(payload, dict):
            if payload.get("NewPrice") is not None:
                result["new_price"] = payload.get("NewPrice")
            if payload.get("OrderIdList") is not None:
                result["order_id_list"] = payload.get("OrderIdList")
        if code in {"5002", "5003", "5004", "5005", "5006", "5007", "6003", "6004", "6005"}:
            result["auth_required"] = True
        return result

    def request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.client.post(path, payload)
        except EcoOpenAPIError as exc:
            return exc.payload

    def sell_goods_list(
        self,
        market_hash_name: str,
        *,
        game_id: str = "730",
        page_index: int = 1,
        page_size: int = 20,
        max_price: float | None = None,
        is_first: bool | None = None,
        is_auto_shipping: bool | None = None,
        good_range: int = 1,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "GameId": str(game_id),
            "HashName": market_hash_name,
            "PageIndex": int(page_index),
            "PageSize": max(1, min(int(page_size), 100)),
            "GoodRange": int(good_range),
        }
        if is_first is not None:
            payload["IsFirst"] = bool(is_first)
        if is_auto_shipping is not None:
            payload["IsAutoShipping"] = bool(is_auto_shipping)
        data = self.client.post("/Api/Market/SellGoodsList", payload)
        if not self.client.is_success(data):
            logger.warning("ECO SellGoodsList failed | item=%s msg=%s", market_hash_name, self.client.result_message(data))
            return []
        rows = _rows_from_page_result(_result_payload(data))
        if max_price is not None and float(max_price) > 0:
            rows = [row for row in rows if _safe_float(row.get("SellingPrice") or row.get("Price")) <= float(max_price)]
        return sorted(rows, key=lambda row: _safe_float(row.get("SellingPrice") or row.get("Price")))

    def select_best_listing(
        self,
        market_hash_name: str,
        *,
        max_price: float,
        game_id: str = "730",
        quantity: int = 1,
    ) -> Optional[dict[str, Any]]:
        rows = self.sell_goods_list(market_hash_name, game_id=game_id, max_price=max_price, page_size=max(20, quantity))
        for row in rows:
            goods_num = row.get("GoodsNum") or row.get("GoodNum") or row.get("goodsNum")
            asset_id = row.get("AssetId") or row.get("AssetID") or row.get("assetId")
            price = _safe_float(row.get("SellingPrice") or row.get("Price"))
            if (goods_num or asset_id) and price > 0 and price <= float(max_price):
                out = dict(row)
                out["_selected_goods_num"] = goods_num
                out["_selected_asset_id"] = asset_id
                out["_selected_price"] = price
                return out
        return None

    def buy_by_goods_num(
        self,
        *,
        goods_num: str | int | None = None,
        asset_id: str | int | None = None,
        price: float,
        trade_link: str,
        merchant_no: str | None = None,
        game_id: str = "730",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "Price": round(float(price), 2),
            "TradeLink": trade_link,
            "MerchantNo": merchant_no or _make_merchant_no("ASECO"),
            "GameId": str(game_id),
        }
        if goods_num:
            payload["GoodsNum"] = str(goods_num)
        if asset_id:
            payload["AssetID"] = str(asset_id)
        if not payload.get("GoodsNum") and not payload.get("AssetID"):
            return {"success": False, "msg": "ECO GoodsNum/AssetID is required", "reason": "mapping_missing"}
        if not trade_link:
            return {"success": False, "msg": "ECO TradeLink is required", "reason": "missing_trade_link"}

        data = self.client.post("/Api/open/buy/BuyByGoodsNum", payload)
        if self.client.is_success(data):
            result = self._ok_result(data, fallback_msg="ECO buy order submitted")
            result["order_id"] = (result.get("data") or {}).get("OrderNo") if isinstance(result.get("data"), dict) else None
            result["merchant_no"] = payload["MerchantNo"]
            return result
        result = self._fail_result(data, fallback_msg="ECO buy order failed")
        result["merchant_no"] = payload["MerchantNo"]
        return result

    def create_buy_order(self, goods_id: str | int | None = None, price: float = 0, num: int = 1, **kwargs: Any) -> dict[str, Any]:
        market_hash_name = kwargs.get("market_hash_name") or kwargs.get("commodity_name") or kwargs.get("hash_name")
        trade_link = kwargs.get("trade_link") or kwargs.get("TradeLink") or kwargs.get("tradeLink") or ""
        game_id = str(kwargs.get("game_id") or kwargs.get("gameId") or "730")
        merchant_no = kwargs.get("merchant_no") or kwargs.get("MerchantNo")

        if goods_id:
            return self.buy_by_goods_num(
                goods_num=goods_id,
                price=float(price),
                trade_link=trade_link,
                merchant_no=merchant_no,
                game_id=game_id,
            )

        if not market_hash_name:
            return {"success": False, "msg": "ECO market_hash_name is required", "reason": "missing_hash_name"}

        listing = self.select_best_listing(market_hash_name, max_price=float(price), game_id=game_id, quantity=int(num))
        if not listing:
            return {
                "success": False,
                "msg": "ECO no listing within target price",
                "reason": "listing_not_found",
                "market_hash_name": market_hash_name,
                "target_price": float(price),
            }
        return self.buy_by_goods_num(
            goods_num=listing.get("_selected_goods_num"),
            asset_id=listing.get("_selected_asset_id"),
            price=listing.get("_selected_price") or price,
            trade_link=trade_link,
            merchant_no=merchant_no,
            game_id=game_id,
        )

    def create_purchase_order(
        self,
        *,
        market_hash_name: str,
        price: float,
        num: int = 1,
        trade_link: str = "",
        steam_id: str = "",
        game_id: str = "730",
        support_presale: bool = False,
        wear_min: float | None = None,
        wear_max: float | None = None,
        style: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "GameId": str(game_id),
            "HashName": market_hash_name,
            "UnitPrice": round(float(price), 2),
            "Count": int(num),
            "IsSupportPreSale": bool(support_presale),
        }
        if steam_id:
            payload["SteamId"] = str(steam_id)
        if trade_link:
            payload["TradeLink"] = str(trade_link)
        if wear_min is not None:
            payload["WearMin"] = float(wear_min)
        if wear_max is not None:
            payload["WearMax"] = float(wear_max)
        if style is not None:
            payload["Style"] = int(style)

        data = self.client.post("/Api/open/purchase/PurchasePublish", payload)
        if self.client.is_success(data):
            result = self._ok_result(data, fallback_msg="ECO purchase order submitted")
            result["purchase_id"] = (result.get("data") or {}).get("PurchaseId") if isinstance(result.get("data"), dict) else None
            return result
        return self._fail_result(data, fallback_msg="ECO purchase order failed")

    def query_order_status(
        self,
        *,
        order_nums: list[str] | None = None,
        merchant_nos: list[str] | None = None,
        game_id: str = "730",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"GameId": str(game_id)}
        if order_nums:
            payload["OrderNums"] = [str(x) for x in order_nums[:100]]
        if merchant_nos:
            payload["MerchantNos"] = [str(x) for x in merchant_nos[:100]]
        data = self.client.post("/Api/open/order/OrderStatusQuery", payload)
        if self.client.is_success(data):
            return self._ok_result(data, fallback_msg="ECO order status loaded")
        return self._fail_result(data, fallback_msg="ECO order status failed")

    def create_listing(
        self,
        *,
        assetid: str | int,
        price: float,
        steam_id: str,
        game_id: str = "730",
        desc: str = "",
    ) -> dict[str, Any]:
        return self._publish_sale_asset(
            assetid=assetid,
            price=price,
            steam_id=steam_id,
            game_id=game_id,
            desc=desc,
            publish_type=1,
            fallback_msg="ECO listing submitted",
        )

    def change_price(
        self,
        *,
        assetid: str | int,
        price: float,
        steam_id: str,
        game_id: str = "730",
        desc: str = "",
    ) -> dict[str, Any]:
        return self._publish_sale_asset(
            assetid=assetid,
            price=price,
            steam_id=steam_id,
            game_id=game_id,
            desc=desc,
            publish_type=2,
            fallback_msg="ECO price change submitted",
        )

    def _publish_sale_asset(
        self,
        *,
        assetid: str | int,
        price: float,
        steam_id: str,
        game_id: str,
        desc: str,
        publish_type: int,
        fallback_msg: str,
    ) -> dict[str, Any]:
        asset_id = str(assetid or "").strip()
        steam_id = str(steam_id or "").strip()
        sell_price = _safe_float(price)
        if not asset_id:
            return {"success": False, "msg": "ECO AssetId is required", "reason": "validation_error"}
        if not steam_id:
            return {"success": False, "msg": "ECO SteamId is required", "reason": "validation_error"}
        if sell_price <= 0:
            return {"success": False, "msg": "ECO SellPrice must be greater than 0", "reason": "validation_error"}

        asset = {
            "AssetId": asset_id,
            "SteamGameId": str(game_id or "730"),
            "TradeTypes": [1],
            "SellPrice": round(sell_price, 2),
            "SellDescription": str(desc or ""),
        }
        payload = {
            "SteamId": steam_id,
            "PublishType": int(publish_type),
            "Assets": [asset],
        }
        data = self.client.post("/Api/Rent/PublishRentAndSaleGoods", payload)
        rows = _operation_rows(data)
        failed_rows = [row for row in rows if not _row_success(row)]
        if self.client.is_success(data) and not failed_rows:
            result = self._ok_result(data, fallback_msg=fallback_msg)
            result["assetid"] = asset_id
            result["platform_listing_id"] = str(_first_row_value(rows, "GoodsNum", "goodsNum", "ListingId", "listing_id") or asset_id)
            result["request_payload"] = payload
            return result
        result = self._fail_result(data, fallback_msg=f"{fallback_msg} failed")
        if failed_rows:
            result["msg"] = str(_first_row_value(failed_rows, "ErrorMsg", "errorMsg", "Message", "message") or result.get("msg") or "")
            result["failed_rows"] = failed_rows
        result["request_payload"] = payload
        return result

    def off_shelf(
        self,
        goods_nums: list[str | int] | tuple[str | int, ...] | str | int | None = None,
        *,
        assetids: list[str | int] | tuple[str | int, ...] | str | int | None = None,
        game_id: str = "730",
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        if isinstance(goods_nums, (str, int)):
            goods_iter = [goods_nums]
        else:
            goods_iter = list(goods_nums or [])
        if isinstance(assetids, (str, int)):
            asset_iter = [assetids]
        else:
            asset_iter = list(assetids or [])

        for goods_num in goods_iter:
            value = str(goods_num or "").strip()
            if value:
                rows.append({"GoodsNum": value, "SteamGameId": str(game_id or "730")})
        for assetid in asset_iter:
            value = str(assetid or "").strip()
            if value:
                rows.append({"AssetId": value, "SteamGameId": str(game_id or "730")})

        if not rows:
            return {"success": False, "msg": "ECO GoodsNum or AssetId is required", "reason": "validation_error"}

        payload = {"goodsNumList": rows}
        data = self.client.post("/Api/Selling/OffshelfGoods", payload)
        result_rows = _operation_rows(data)
        failed_rows = [row for row in result_rows if not _row_success(row)]
        if self.client.is_success(data) and not failed_rows:
            result = self._ok_result(data, fallback_msg="ECO goods off-shelf submitted")
            result["platform_listing_id"] = str(_first_row_value(result_rows, "GoodsNum", "goodsNum") or rows[0].get("GoodsNum") or rows[0].get("AssetId") or "")
            result["assetid"] = str(_first_row_value(result_rows, "AssetId", "AssetID", "assetId") or rows[0].get("AssetId") or "")
            result["request_payload"] = payload
            return result
        result = self._fail_result(data, fallback_msg="ECO off-shelf failed")
        if failed_rows:
            result["msg"] = str(_first_row_value(failed_rows, "ErrorMsg", "errorMsg", "Message", "message") or result.get("msg") or "")
            result["failed_rows"] = failed_rows
        result["request_payload"] = payload
        return result

    def buy(self, *args: Any, **kwargs: Any) -> Any:
        return self.create_buy_order(*args, **kwargs)

    def sell(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ECO sell/listing flow should use PublishStock explicitly")

    def query_inventory(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("ECO inventory query is not implemented yet")

    def search_item_id_by_name(self, market_hash_name: str) -> Optional[str]:
        listing = self.select_best_listing(market_hash_name, max_price=float("inf"))
        value = listing.get("_selected_goods_num") if listing else None
        return str(value) if value else None
