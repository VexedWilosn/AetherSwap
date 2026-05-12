import json
from concurrent.futures import ThreadPoolExecutor

from app.services.platform_sessions import PlatformClientFactory, PlatformSessionStateStore


_UUYP_TOKEN_WITH_DEVICE = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJkZXZpY2VJZCI6ImRldmljZS0xIn0."
    "signature"
)


def _eco_credentials(*, trade_link: str = "") -> dict:
    payload = {
        "PartnerId": "partner-demo",
        "RsaPrivateKey": "-----BEGIN PRIVATE KEY-----\nunit-test\n-----END PRIVATE KEY-----",
    }
    if trade_link:
        payload["trade_link"] = trade_link
    return {"eco_openapi": payload}


def test_eco_preflight_requires_trade_link_for_direct_buy(tmp_path):
    factory = PlatformClientFactory(
        credentials=_eco_credentials(),
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("eco", purpose="direct_buy")

    assert preflight.ok is False
    assert preflight.reason == "missing_trade_link"
    assert preflight.status == "missing"
    state = factory.store.get("eco")
    assert state.last_error_code == "missing_trade_link"


def test_eco_preflight_allows_purchase_order_without_trade_link(tmp_path):
    factory = PlatformClientFactory(
        credentials=_eco_credentials(),
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("eco", purpose="purchase_order")

    assert preflight.ok is True


def test_eco_preflight_allows_direct_buy_with_trade_link(tmp_path):
    factory = PlatformClientFactory(
        credentials=_eco_credentials(
            trade_link="https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
        ),
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("eco", purpose="direct_buy")

    assert preflight.ok is True


def test_eco_preflight_allows_direct_buy_with_steam_trade_link(tmp_path):
    credentials = _eco_credentials()
    credentials["steam"] = {
        "trade_link": "https://steamcommunity.com/tradeoffer/new/?partner=1&token=abc",
    }
    factory = PlatformClientFactory(
        credentials=credentials,
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("eco", purpose="direct_buy")

    assert preflight.ok is True


def test_uuyp_preflight_parses_token_from_cookie_blob(tmp_path):
    factory = PlatformClientFactory(
        credentials={
            "uuyp": {
                "cookies": f"uu_token={_UUYP_TOKEN_WITH_DEVICE}; uk=uk-1; deviceUk=device-uk-1"
            }
        },
        config={},
        store=PlatformSessionStateStore(tmp_path / "platform_session_state.json"),
    )

    preflight = factory.preflight("uuyp", purpose="purchase_order")

    assert preflight.ok is True


def test_state_store_handles_concurrent_status_writes(tmp_path):
    path = tmp_path / "platform_session_state.json"

    def write_state(index: int) -> None:
        store = PlatformSessionStateStore(path)
        if index % 3 == 0:
            store.mark_valid("buff", "cookie", cookies={"session": f"buff-{index}"})
        elif index % 3 == 1:
            store.mark_success("uuyp")
        else:
            store.mark_error("eco", "business_error", f"not found {index}", status="error")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_state, range(48)))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"buff", "uuyp", "eco"}.issubset(data.keys())
    assert not list(tmp_path.glob("*.tmp"))
