from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ORDER_STATUS_UNKNOWN = "unknown"
ORDER_STATUS_PENDING = "pending"
ORDER_STATUS_COMPLETED = "completed"
ORDER_STATUS_FAILED = "failed"


_PENDING_TOKENS = {
    "active",
    "created",
    "creating",
    "delivering",
    "matching",
    "open",
    "paid",
    "paying",
    "pending",
    "processing",
    "submitted",
    "to_confirm",
    "to_deliver",
    "to_receive",
    "wait",
    "wait_buyer_confirm",
    "wait_confirm",
    "wait_pay",
    "wait_seller_send_offer",
    "wait_send",
    "waiting",
    "待付款",
    "待发货",
    "待报价",
    "待处理",
    "进行中",
    "求购中",
    "已付款",
}

_COMPLETED_TOKENS = {
    "accept",
    "accepted",
    "complete",
    "completed",
    "deal",
    "delivered",
    "done",
    "filled",
    "finish",
    "finished",
    "received",
    "settled",
    "success",
    "succeeded",
    "trade_success",
    "成交",
    "交易成功",
    "已完成",
    "已成交",
    "已收货",
    "成功",
}

_FAILED_TOKENS = {
    "cancel",
    "canceled",
    "cancelled",
    "closed",
    "declined",
    "expired",
    "fail",
    "failed",
    "invalid",
    "invaliditems",
    "reject",
    "rejected",
    "timeout",
    "取消",
    "关闭",
    "失败",
    "已取消",
    "已关闭",
    "已拒绝",
    "过期",
}

_NUMERIC_STATUS_BY_PLATFORM: dict[str, dict[str, str]] = {
    "steam_trade_offer": {
        "1": ORDER_STATUS_FAILED,
        "2": ORDER_STATUS_PENDING,
        "3": ORDER_STATUS_COMPLETED,
        "4": ORDER_STATUS_PENDING,
        "5": ORDER_STATUS_FAILED,
        "6": ORDER_STATUS_FAILED,
        "7": ORDER_STATUS_FAILED,
        "8": ORDER_STATUS_FAILED,
        "9": ORDER_STATUS_PENDING,
        "10": ORDER_STATUS_FAILED,
        "11": ORDER_STATUS_PENDING,
    },
    "steam": {
        "1": ORDER_STATUS_COMPLETED,
        "true": ORDER_STATUS_COMPLETED,
    },
    "buff": {
        "1": ORDER_STATUS_PENDING,
        "2": ORDER_STATUS_PENDING,
        "3": ORDER_STATUS_COMPLETED,
        "4": ORDER_STATUS_FAILED,
        "5": ORDER_STATUS_FAILED,
    },
    "eco": {
        "3": ORDER_STATUS_FAILED,
        "30": ORDER_STATUS_COMPLETED,
    },
    "c5game": {
        "0": ORDER_STATUS_PENDING,
        "1": ORDER_STATUS_PENDING,
        "2": ORDER_STATUS_PENDING,
        "3": ORDER_STATUS_PENDING,
        "10": ORDER_STATUS_COMPLETED,
        "11": ORDER_STATUS_FAILED,
    },
}


_STATUS_KEYS = (
    "order_status",
    "orderStatus",
    "OrderStatus",
    "order_state",
    "orderState",
    "OrderState",
    "order_state_code",
    "orderStateCode",
    "OrderStateCode",
    "purchase_status",
    "purchaseStatus",
    "PurchaseStatus",
    "trade_offer_state",
    "tradeOfferState",
    "TradeOfferState",
    "state",
    "State",
    "status",
    "Status",
)

_ID_KEYS = {
    "platform_order_id": (
        "platform_order_id",
        "order_id",
        "orderId",
        "OrderId",
        "OrderID",
        "OrderNo",
        "orderNo",
        "OrderNum",
        "orderNum",
        "buy_order_id",
        "buy_orderid",
        "BuyOrderId",
        "PurchaseId",
        "purchaseId",
        "purchase_id",
        "MerchantNo",
        "merchantNo",
        "bill_order_id",
        "id",
    ),
    "platform_listing_id": (
        "platform_listing_id",
        "listing_id",
        "listingid",
        "listingId",
        "ListingId",
        "sell_order_id",
        "SellOrderId",
    ),
    "trade_offer_id": (
        "tradeofferid",
        "trade_offer_id",
        "tradeOfferId",
        "TradeOfferId",
        "offer_id",
        "offerId",
        "OfferId",
    ),
    "assetid": (
        "assetid",
        "asset_id",
        "AssetId",
        "AssetID",
    ),
}


_FILLED_QUANTITY_KEYS = (
    "filled_quantity",
    "filledQuantity",
    "filled_num",
    "filledNum",
    "filled",
    "fill_num",
    "fillNum",
    "deal_num",
    "dealNum",
    "dealed_num",
    "success_num",
    "successNum",
    "complete_num",
    "completeNum",
    "completed_num",
    "completedNum",
    "matched_num",
    "matchedNum",
    "成交数量",
)

_REMAINING_QUANTITY_KEYS = (
    "remaining_quantity",
    "remainingQuantity",
    "remain_quantity",
    "remainQuantity",
    "remaining_num",
    "remainingNum",
    "remain_num",
    "remainNum",
    "left_num",
    "leftNum",
    "unfilled_num",
    "unfilledNum",
    "pending_num",
    "pendingNum",
    "待成交数量",
)

_TOTAL_QUANTITY_KEYS = (
    "quantity",
    "Quantity",
    "num",
    "Num",
    "order_num",
    "orderNum",
    "buy_num",
    "buyNum",
    "sell_num",
    "sellNum",
    "total_num",
    "totalNum",
    "count",
    "Count",
    "qty",
    "Qty",
    "数量",
)

_FILLED_AMOUNT_KEYS = (
    "filled_amount_cny",
    "filledAmountCny",
    "filled_amount",
    "filledAmount",
    "deal_amount",
    "dealAmount",
    "success_amount",
    "successAmount",
    "paid_amount",
    "paidAmount",
    "成交金额",
)

_REMAINING_AMOUNT_KEYS = (
    "remaining_amount_cny",
    "remainingAmountCny",
    "remaining_amount",
    "remainingAmount",
    "remain_amount",
    "remainAmount",
    "unfilled_amount",
    "unfilledAmount",
    "待成交金额",
)


@dataclass(frozen=True)
class OrderStatusSnapshot:
    status: str = ORDER_STATUS_UNKNOWN
    platform_order_id: str | None = None
    platform_listing_id: str | None = None
    trade_offer_id: str | None = None
    assetid: str | None = None
    filled_quantity: int | None = None
    remaining_quantity: int | None = None
    filled_amount_cny: float | None = None
    remaining_amount_cny: float | None = None
    matched: dict[str, Any] | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == ORDER_STATUS_PENDING

    @property
    def is_completed(self) -> bool:
        return self.status == ORDER_STATUS_COMPLETED

    @property
    def is_failed(self) -> bool:
        return self.status == ORDER_STATUS_FAILED


def normalize_order_status(payload: Any, *, platform: str = "", expected_order_id: str = "") -> OrderStatusSnapshot:
    rows = _candidate_rows(payload)
    matched = _match_expected_row(rows, expected_order_id) if expected_order_id else None
    status_source = matched or _first_row_with_status(rows) or (rows[0] if rows else payload)
    platform_key = _platform_key(platform, status_source)
    status = _classify_status(status_source, platform_key)

    id_source = matched or _first_row_with_any_id(rows) or (payload if isinstance(payload, dict) else {})
    fill_source = matched or _first_row_with_fill(rows) or id_source or (payload if isinstance(payload, dict) else {})
    filled_quantity = _first_int(fill_source, _FILLED_QUANTITY_KEYS)
    remaining_quantity = _first_int(fill_source, _REMAINING_QUANTITY_KEYS)
    total_quantity = _first_int(fill_source, _TOTAL_QUANTITY_KEYS)
    if remaining_quantity is None and filled_quantity is not None and total_quantity is not None:
        remaining_quantity = max(0, int(total_quantity) - int(filled_quantity))
    if filled_quantity is None and remaining_quantity is not None and total_quantity is not None:
        filled_quantity = max(0, int(total_quantity) - int(remaining_quantity))
    if status == ORDER_STATUS_COMPLETED and filled_quantity is None and total_quantity is not None:
        filled_quantity = max(0, int(total_quantity))
        remaining_quantity = 0
    if filled_quantity is not None and remaining_quantity is not None:
        if filled_quantity > 0 and remaining_quantity <= 0:
            status = ORDER_STATUS_COMPLETED
        elif filled_quantity > 0 and remaining_quantity > 0:
            status = ORDER_STATUS_PENDING
        elif status == ORDER_STATUS_PENDING and filled_quantity <= 0 and remaining_quantity <= 0 and total_quantity is not None:
            remaining_quantity = max(0, int(total_quantity))

    return OrderStatusSnapshot(
        status=status,
        platform_order_id=_first_id(id_source, "platform_order_id") or _first_id(payload, "platform_order_id"),
        platform_listing_id=_first_id(id_source, "platform_listing_id") or _first_id(payload, "platform_listing_id"),
        trade_offer_id=_first_id(id_source, "trade_offer_id") or _first_id(payload, "trade_offer_id"),
        assetid=_first_id(id_source, "assetid") or _first_id(payload, "assetid"),
        filled_quantity=filled_quantity,
        remaining_quantity=remaining_quantity,
        filled_amount_cny=_first_float(fill_source, _FILLED_AMOUNT_KEYS),
        remaining_amount_cny=_first_float(fill_source, _REMAINING_AMOUNT_KEYS),
        matched=matched,
    )


def _platform_key(platform: str, row: Any) -> str:
    key = str(platform or "").lower().strip()
    if key == "steam" and isinstance(row, dict) and any(k in row for k in ("trade_offer_state", "tradeOfferState", "TradeOfferState")):
        return "steam_trade_offer"
    return key


def _candidate_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows: list[dict[str, Any]] = [payload]
        for key in ("data", "Data", "ResultData", "resultData", "orderConfirmInfoDTO", "asset_info", "assetInfo", "raw"):
            nested = payload.get(key)
            if nested is payload:
                continue
            if isinstance(nested, list):
                rows.extend(row for row in nested if isinstance(row, dict))
                continue
            rows.extend(_candidate_rows(nested))
        for key in ("items", "Items", "rows", "Rows", "list", "List", "orders", "Orders", "PageResult", "pageResult"):
            nested = payload.get(key)
            if isinstance(nested, list):
                rows.extend(row for row in nested if isinstance(row, dict))
            elif isinstance(nested, dict):
                rows.extend(_candidate_rows(nested))
        return rows
    if isinstance(payload, list):
        rows = []
        for item in payload:
            if isinstance(item, dict):
                rows.extend(_candidate_rows(item))
        return rows
    return []


def _match_expected_row(rows: list[dict[str, Any]], expected_order_id: str) -> dict[str, Any] | None:
    expected = str(expected_order_id or "").strip()
    if not expected:
        return None
    for row in rows:
        for key in _ID_KEYS["platform_order_id"]:
            if str(row.get(key) or "").strip() == expected:
                return row
    return None


def _first_row_with_any_id(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if any(_first_id(row, field) for field in _ID_KEYS):
            return row
    return None


def _first_row_with_status(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if any(row.get(key) not in (None, "") for key in _STATUS_KEYS):
            return row
    return None


def _first_row_with_fill(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    keys = _FILLED_QUANTITY_KEYS + _REMAINING_QUANTITY_KEYS + _FILLED_AMOUNT_KEYS + _REMAINING_AMOUNT_KEYS
    for row in rows:
        if any(row.get(key) not in (None, "") for key in keys):
            return row
    return None


def _first_id(payload: Any, field: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _ID_KEYS[field]:
        value = payload.get(key)
        if value not in (None, ""):
            token = str(value).strip()
            if token and token.lower() not in {"none", "null", "undefined", "nan"}:
                return token
    for nested_key in ("asset_info", "assetInfo"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            found = _first_id(nested, field)
            if found:
                return found
    return None


def _first_int(payload: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return max(0, int(float(str(value).replace(",", "").strip())))
        except (TypeError, ValueError):
            continue
    return None


def _first_float(payload: Any, keys: tuple[str, ...]) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return round(max(0.0, float(str(value).replace(",", "").strip())), 2)
        except (TypeError, ValueError):
            continue
    return None


def _classify_status(payload: Any, platform: str) -> str:
    if not isinstance(payload, dict):
        return ORDER_STATUS_UNKNOWN
    values = []
    for key in _STATUS_KEYS:
        if payload.get(key) not in (None, ""):
            values.append(payload.get(key))
    if not values:
        return ORDER_STATUS_UNKNOWN

    for value in values:
        mapped = _classify_single_value(value, platform)
        if mapped != ORDER_STATUS_UNKNOWN:
            return mapped
    return ORDER_STATUS_UNKNOWN


def _classify_single_value(value: Any, platform: str) -> str:
    text = str(value).strip().lower()
    if text in _NUMERIC_STATUS_BY_PLATFORM.get(platform, {}):
        return _NUMERIC_STATUS_BY_PLATFORM[platform][text]
    if text in _NUMERIC_STATUS_BY_PLATFORM.get("steam_trade_offer", {}) and platform == "steam_trade_offer":
        return _NUMERIC_STATUS_BY_PLATFORM["steam_trade_offer"][text]
    if any(token in text for token in _FAILED_TOKENS):
        return ORDER_STATUS_FAILED
    if any(token in text for token in _COMPLETED_TOKENS):
        return ORDER_STATUS_COMPLETED
    if any(token in text for token in _PENDING_TOKENS):
        return ORDER_STATUS_PENDING
    return ORDER_STATUS_UNKNOWN
