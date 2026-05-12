from app.services.trading.adapters import normalize_platform_result
from app.services.trading.order_status import (
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_FAILED,
    ORDER_STATUS_PENDING,
    normalize_order_status,
)


def test_order_status_normalizes_eco_order_status_rows_by_expected_id():
    payload = {
        "success": True,
        "data": [
            {"OrderNo": "eco-1", "OrderStatus": "Processing"},
            {"OrderNo": "eco-2", "OrderStatus": "Completed", "AssetId": "asset-2"},
        ],
    }

    status = normalize_order_status(payload, platform="eco", expected_order_id="eco-2")

    assert status.status == ORDER_STATUS_COMPLETED
    assert status.platform_order_id == "eco-2"
    assert status.assetid == "asset-2"


def test_order_status_prefers_eco_order_num_over_merchant_no_and_marks_protection_completed():
    payload = {
        "success": True,
        "data": {
            "OrderNum": "2026011188366102982574101",
            "MerchantNo": "ASECO1778534001949",
            "OrderState": "交易保护",
            "OrderStateCode": 30,
            "TradeOfferId": "9079998096",
            "AssetId": "50892957247",
        },
    }

    status = normalize_order_status(payload, platform="eco", expected_order_id="2026011188366102982574101")
    result = normalize_platform_result(payload, platform="eco", expected_order_id="2026011188366102982574101")

    assert status.status == ORDER_STATUS_COMPLETED
    assert status.platform_order_id == "2026011188366102982574101"
    assert status.trade_offer_id == "9079998096"
    assert status.assetid == "50892957247"
    assert result.category == "order_completed"
    assert result.platform_order_id == "2026011188366102982574101"


def test_order_status_ignores_string_none_offer_and_clears_cancelled_eco_asset():
    result = normalize_platform_result(
        {
            "success": True,
            "data": {
                "OrderNum": "eco-cancelled-1",
                "MerchantNo": "ASECO-cancelled-1",
                "OrderState": "交易取消",
                "OrderStateCode": 3,
                "TradeOfferId": "None",
                "AssetId": "not-owned-asset",
            },
        },
        platform="eco",
        expected_order_id="eco-cancelled-1",
    )

    assert result.category == "cancelled"
    assert result.platform_order_id == "eco-cancelled-1"
    assert result.trade_offer_id is None
    assert result.assetid is None


def test_order_status_keeps_steam_active_buy_order_pending():
    payload = {
        "success": True,
        "data": {
            "order_id": "steam-1",
            "order_status": "open",
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
        },
    }

    status = normalize_order_status(payload, platform="steam", expected_order_id="steam-1")

    assert status.status == ORDER_STATUS_PENDING
    assert status.platform_order_id == "steam-1"


def test_order_status_normalizes_steam_trade_offer_state_ints():
    accepted = normalize_order_status(
        {"success": True, "data": {"tradeofferid": "offer-1", "trade_offer_state": 3}},
        platform="steam",
    )
    active = normalize_order_status(
        {"success": True, "data": {"tradeofferid": "offer-2", "trade_offer_state": 2}},
        platform="steam",
    )
    declined = normalize_order_status(
        {"success": True, "data": {"tradeofferid": "offer-3", "trade_offer_state": 7}},
        platform="steam",
    )

    assert accepted.status == ORDER_STATUS_COMPLETED
    assert active.status == ORDER_STATUS_PENDING
    assert declined.status == ORDER_STATUS_FAILED


def test_normalize_platform_result_uses_order_status_matrix():
    pending = normalize_platform_result(
        {"success": True, "data": {"OrderNo": "uuyp-1", "orderStatus": "wait_send"}},
        platform="uuyp",
        expected_order_id="uuyp-1",
    )
    completed = normalize_platform_result(
        {"success": True, "data": {"OrderNo": "eco-1", "OrderStatus": "finished"}},
        platform="eco",
        expected_order_id="eco-1",
    )
    failed = normalize_platform_result(
        {"success": True, "data": {"OrderNo": "buff-1", "state": "cancelled"}},
        platform="buff",
        expected_order_id="buff-1",
    )

    assert pending.category == "order_pending"
    assert pending.platform_order_id == "uuyp-1"
    assert completed.category == "order_completed"
    assert failed.category == "cancelled"


def test_buff_to_deliver_order_extracts_offer_and_asset_info():
    payload = {
        "success": True,
        "data": {
            "id": "buff-order-1",
            "state": "TO_DELIVER",
            "tradeofferid": "offer-1",
            "asset_info": {"assetid": "asset-1"},
        },
    }

    status = normalize_order_status(payload, platform="buff", expected_order_id="buff-order-1")
    result = normalize_platform_result(payload, platform="buff", expected_order_id="buff-order-1")

    assert status.status == ORDER_STATUS_PENDING
    assert status.platform_order_id == "buff-order-1"
    assert status.trade_offer_id == "offer-1"
    assert status.assetid == "asset-1"
    assert result.category == "order_pending"
    assert result.trade_offer_id == "offer-1"
    assert result.assetid == "asset-1"


def test_cancelled_platform_result_does_not_claim_buff_asset_info_as_owned():
    result = normalize_platform_result(
        {
            "success": True,
            "data": {
                "id": "buff-order-cancelled",
                "state": "FAIL",
                "asset_info": {"assetid": "not-owned-asset"},
            },
        },
        platform="buff",
        expected_order_id="buff-order-cancelled",
    )

    assert result.category == "cancelled"
    assert result.assetid is None


def test_normalize_platform_result_classifies_underscore_not_found_reason():
    result = normalize_platform_result(
        {
            "success": False,
            "reason": "not_found",
            "msg": "No BUFF sell order at or below target price",
        },
        platform="buff",
    )

    assert result.category == "not_found"


def test_order_status_extracts_partial_fill_progress():
    payload = {
        "success": True,
        "data": {
            "OrderNo": "eco-partial-1",
            "OrderStatus": "processing",
            "num": 5,
            "deal_num": 2,
            "remain_num": 3,
            "deal_amount": "246.8",
        },
    }

    status = normalize_order_status(payload, platform="eco", expected_order_id="eco-partial-1")
    result = normalize_platform_result(payload, platform="eco", expected_order_id="eco-partial-1")

    assert status.status == ORDER_STATUS_PENDING
    assert status.filled_quantity == 2
    assert status.remaining_quantity == 3
    assert status.filled_amount_cny == 246.8
    assert result.category == "order_pending"
    assert result.filled_quantity == 2
    assert result.remaining_quantity == 3
    assert result.filled_amount_cny == 246.8


def test_order_status_marks_fully_filled_progress_completed():
    payload = {
        "success": True,
        "data": {
            "OrderNo": "eco-full-1",
            "OrderStatus": "processing",
            "num": 2,
            "filled_num": 2,
            "remaining_num": 0,
        },
    }

    result = normalize_platform_result(payload, platform="eco", expected_order_id="eco-full-1")

    assert result.category == "order_completed"
    assert result.success is True
    assert result.filled_quantity == 2
    assert result.remaining_quantity == 0
