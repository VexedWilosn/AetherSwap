from __future__ import annotations


class PlatformActionState:
    QUEUED = "queued"
    PROCESSING = "processing"
    SUBMITTED = "submitted"
    WAITING_PLATFORM = "waiting_platform"
    WAITING_TRADE_OFFER = "waiting_trade_offer"
    WAITING_STEAM_CONFIRM = "waiting_steam_confirm"
    WAITING_SETTLEMENT = "waiting_settlement"
    RETRY_WAIT = "retry_wait"
    RISK_BLOCKED = "risk_blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PlatformActionType:
    DIRECT_BUY = "direct_buy"
    PURCHASE_ORDER = "purchase_order"
    STEAM_BUY_ORDER = "steam_buy_order"
    STEAM_LISTING = "steam_listing"
    PLATFORM_LISTING = "platform_listing"
    REPRICE_LISTING = "reprice_listing"
    DELIVER_ORDER = "deliver_order"
    CANCEL_ORDER = "cancel_order"
    ACCEPT_TRADE_OFFER = "accept_trade_offer"
    POLL_ORDER = "poll_order"


TERMINAL_STATES = {
    PlatformActionState.SUCCEEDED,
    PlatformActionState.FAILED,
    PlatformActionState.CANCELLED,
    PlatformActionState.EXPIRED,
}

CLAIMABLE_STATES = {
    PlatformActionState.QUEUED,
    PlatformActionState.SUBMITTED,
    PlatformActionState.WAITING_PLATFORM,
    PlatformActionState.WAITING_TRADE_OFFER,
    PlatformActionState.WAITING_STEAM_CONFIRM,
    PlatformActionState.WAITING_SETTLEMENT,
    PlatformActionState.RETRY_WAIT,
    PlatformActionState.PROCESSING,
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PlatformActionState.QUEUED: {
        PlatformActionState.PROCESSING,
        PlatformActionState.RISK_BLOCKED,
        PlatformActionState.CANCELLED,
        PlatformActionState.FAILED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.RISK_BLOCKED: {
        PlatformActionState.QUEUED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.PROCESSING: {
        PlatformActionState.SUBMITTED,
        PlatformActionState.WAITING_PLATFORM,
        PlatformActionState.WAITING_TRADE_OFFER,
        PlatformActionState.WAITING_STEAM_CONFIRM,
        PlatformActionState.WAITING_SETTLEMENT,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.RISK_BLOCKED,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.SUBMITTED: {
        PlatformActionState.PROCESSING,
        PlatformActionState.WAITING_PLATFORM,
        PlatformActionState.WAITING_TRADE_OFFER,
        PlatformActionState.WAITING_STEAM_CONFIRM,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.WAITING_PLATFORM: {
        PlatformActionState.PROCESSING,
        PlatformActionState.WAITING_TRADE_OFFER,
        PlatformActionState.WAITING_STEAM_CONFIRM,
        PlatformActionState.WAITING_SETTLEMENT,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.WAITING_TRADE_OFFER: {
        PlatformActionState.PROCESSING,
        PlatformActionState.WAITING_STEAM_CONFIRM,
        PlatformActionState.WAITING_SETTLEMENT,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.WAITING_STEAM_CONFIRM: {
        PlatformActionState.PROCESSING,
        PlatformActionState.WAITING_SETTLEMENT,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.WAITING_SETTLEMENT: {
        PlatformActionState.PROCESSING,
        PlatformActionState.SUCCEEDED,
        PlatformActionState.RETRY_WAIT,
        PlatformActionState.FAILED,
        PlatformActionState.CANCELLED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.RETRY_WAIT: {
        PlatformActionState.PROCESSING,
        PlatformActionState.CANCELLED,
        PlatformActionState.FAILED,
        PlatformActionState.EXPIRED,
    },
    PlatformActionState.SUCCEEDED: set(),
    PlatformActionState.FAILED: set(),
    PlatformActionState.CANCELLED: set(),
    PlatformActionState.EXPIRED: set(),
}


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_STATES


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return True
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())
