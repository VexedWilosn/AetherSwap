from app.services.trading.capabilities import (
    CAPABILITY_REGISTRY,
    CAP_CANCEL,
    CAP_DELIVER_ORDER,
    CAP_DIRECT_BUY,
    CAP_ORDER_STATUS,
    CAP_PLATFORM_LISTING,
    CAP_PURCHASE_ORDER,
    CAP_REPRICE,
    CAP_STEAM_LISTING,
    CAP_TRADE_OFFER_ACCEPT,
    CAP_TRADE_OFFER_POLL,
    STATUS_PARTIAL,
    STATUS_PLANNED,
    get_platform_capabilities,
    missing_capabilities,
    supports,
)


def test_capability_registry_covers_target_platforms():
    assert {"buff", "uuyp", "eco", "c5game", "steam"}.issubset(CAPABILITY_REGISTRY)

    assert supports("buff", CAP_DIRECT_BUY)
    assert supports("buff", CAP_TRADE_OFFER_POLL)
    assert supports("buff", CAP_TRADE_OFFER_ACCEPT)
    assert supports("uuyp", CAP_PURCHASE_ORDER)
    assert supports("eco", CAP_PURCHASE_ORDER)
    assert supports("eco", CAP_PLATFORM_LISTING)
    assert supports("eco", CAP_REPRICE)
    assert supports("eco", CAP_CANCEL)
    assert supports("steam", CAP_STEAM_LISTING)


def test_registry_marks_foundation_gaps_as_planned():
    c5 = get_platform_capabilities("c5")
    assert c5.platform == "c5game"
    assert c5.capabilities[CAP_DIRECT_BUY].status == STATUS_PLANNED
    assert supports("c5game", CAP_ORDER_STATUS)
    assert supports("c5game", CAP_DELIVER_ORDER)
    assert supports("c5game", CAP_TRADE_OFFER_POLL)

    buff_missing = missing_capabilities("buff")
    assert CAP_PLATFORM_LISTING in buff_missing

    eco = get_platform_capabilities("eco")
    assert eco.capabilities[CAP_PLATFORM_LISTING].status == STATUS_PARTIAL
