from app.services.steam_trade_link import (
    extract_steam_trade_link_from_text,
    fetch_steam_trade_link,
    steam_id_from_cookie_header,
)


class FakeCookies:
    def __init__(self):
        self.values = {}

    def update(self, values):
        self.values.update(values)


class FakeResponse:
    def __init__(self, text, *, status_code=200, url="https://steamcommunity.com/my/tradeoffers/privacy"):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.cookies = FakeCookies()
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_extract_steam_trade_link_from_privacy_page_html():
    link = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc_DEF-9"
    html = f'<input value="{link.replace("&", "&amp;")}">'

    assert extract_steam_trade_link_from_text(html) == link


def test_steam_id_from_cookie_header_extracts_login_secure_prefix():
    assert steam_id_from_cookie_header("sessionid=sid; steamLoginSecure=76561198000000000%7C%7Ctoken") == "76561198000000000"


def test_fetch_steam_trade_link_uses_logged_in_cookie_and_privacy_page():
    link = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc"
    session = FakeSession(FakeResponse(f"copy this {link}"))

    result = fetch_steam_trade_link(
        "sessionid=sid; steamLoginSecure=76561198000000000%7C%7Ctoken",
        session=session,
    )

    assert result.ok is True
    assert result.trade_link == link
    assert session.cookies.values["steamLoginSecure"] == "76561198000000000%7C%7Ctoken"
    assert session.calls[0][0] == "https://steamcommunity.com/profiles/76561198000000000/tradeoffers/privacy"


def test_fetch_steam_trade_link_requires_steam_login_secure():
    result = fetch_steam_trade_link("sessionid=sid")

    assert result.ok is False
    assert result.reason == "steam_auth_required"
