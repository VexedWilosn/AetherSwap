"""Helpers for syncing Steam account wallet currency and store region."""
from datetime import datetime, timezone
from typing import Optional

from app.accounts import get_account, get_current_account, update_account
from app.config_loader import get_steam_credentials


def sync_account_currency_region(account_id: Optional[str] = None, cookies_str: str = "") -> dict:
    account = get_account(account_id) if account_id else get_current_account()
    if not account:
        return {"ok": False, "error": "账号不存在"}
    cookies = cookies_str or (get_steam_credentials().get("cookies") or "")
    if not cookies:
        return {"ok": False, "error": "缺少 Steam Cookie"}
    from app.gift_engine import get_base_auth_status, get_wallet_balance

    _, country_code, _ = get_base_auth_status(cookies)
    wallet = get_wallet_balance(cookies)
    currency_code = (wallet.get("currency_code") or "").strip().upper()
    region_code = (country_code or "").strip().upper()
    if not currency_code:
        return {"ok": False, "error": "未解析到结算币种"}
    balance_raw = wallet.get("balance_raw")
    balance_value = None
    try:
        balance_value = float(balance_raw) / 100.0 if balance_raw is not None else None
    except (TypeError, ValueError):
        balance_value = None
    updates = {
        "currency_code": currency_code,
        "balance_display": wallet.get("balance_display") or "",
        "wallet_balance": balance_raw,
        "balance": balance_value,
        "wallet_currency_id": wallet.get("currency_id"),
        "wallet_currency_symbol": wallet.get("currency_symbol") or "",
        "balance_synced_at": datetime.now(timezone.utc).isoformat(),
    }
    if region_code:
        updates["region_code"] = region_code
    updated = update_account(account.get("id"), **updates)
    if not updated:
        return {"ok": False, "error": "写入账号配置失败"}
    return {
        "ok": True,
        "currency_code": currency_code,
        "region_code": region_code,
        "balance_display": updates["balance_display"],
        "account": updated,
    }
