import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_steam_login_connection_error_message_is_explicit():
    from app.services import steam_auth

    exc = steam_auth._req.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='steamcommunity.com', port=443): "
        "Max retries exceeded with url: / "
        "(Caused by NewConnectionError: failed to establish a new connection)"
    )

    msg = steam_auth._classify_steam_login_exception(exc)

    assert msg.startswith("network_error:")
    assert "steamcommunity.com:443" in msg
    assert "不是账号密码" in msg
    assert "Steam Guard" in msg
    assert "原始错误" in msg
