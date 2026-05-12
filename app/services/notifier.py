from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_JSON = BASE_DIR / "config" / "config.json"
APP_CONFIG_JSON = BASE_DIR / "config" / "app_config.json"
logger = logging.getLogger(__name__)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}") or {}
    except Exception as exc:
        logger.warning("notifier config read failed | path=%s err=%s", path, exc)
        return {}


def _load_notify_config() -> dict[str, Any]:
    cfg = _load_json(CONFIG_JSON)
    app_cfg = _load_json(APP_CONFIG_JSON)
    notify = {}
    for source in (cfg.get("notify"), cfg.get("notification"), cfg, app_cfg.get("notify"), app_cfg.get("notification")):
        if isinstance(source, dict):
            notify.update(source)
    return notify


def _webhook_url(cfg: dict[str, Any]) -> str:
    return str(
        cfg.get("webhook_url")
        or cfg.get("WEBHOOK_URL")
        or cfg.get("serverchan_url")
        or cfg.get("pushplus_url")
        or cfg.get("telegram_webhook_url")
        or ""
    ).strip()


def is_enabled() -> bool:
    cfg = _load_notify_config()
    enabled = cfg.get("enabled", cfg.get("ENABLE_WEBHOOK", True))
    return bool(_webhook_url(cfg)) and str(enabled).lower() not in {"0", "false", "no", "off"}


def notify_webhook(title: str, content: str, *, extra: dict[str, Any] | None = None) -> bool:
    cfg = _load_notify_config()
    url = _webhook_url(cfg)
    if not url or not is_enabled():
        return False
    payload = {
        "title": title,
        "text": content,
        "content": content,
        "desp": content,
        "message": content,
        "extra": extra or {},
    }
    try:
        response = requests.post(url, json=payload, timeout=8)
        if response.status_code >= 400:
            response = requests.post(url, data=payload, timeout=8)
        ok = response.status_code < 400
        if not ok:
            logger.warning("webhook notifier failed | status=%s body=%s", response.status_code, response.text[:200])
        return ok
    except Exception as exc:
        logger.warning("webhook notifier exception: %s", exc)
        return False


def notify_trade_success(
    *,
    item_name: str,
    action: str,
    price: float,
    platform: str,
    quantity: int = 1,
    extra: dict[str, Any] | None = None,
) -> bool:
    cfg = _load_notify_config()
    url = _webhook_url(cfg)
    if not url:
        return False
    if not is_enabled():
        return False

    title = f"AetherSwap 下单成功: {platform}"
    content = (
        f"饰品: {item_name}\n"
        f"操作: {action}\n"
        f"平台: {platform}\n"
        f"价格: ¥{float(price or 0):.2f}\n"
        f"数量: {int(quantity or 1)}"
    )
    payload = {
        "title": title,
        "text": content,
        "content": content,
        "desp": content,
        "message": content,
        "item_name": item_name,
        "action": action,
        "platform": platform,
        "price": float(price or 0),
        "quantity": int(quantity or 1),
        "extra": extra or {},
    }

    try:
        response = requests.post(url, json=payload, timeout=8)
        if response.status_code >= 400:
            response = requests.post(url, data=payload, timeout=8)
        ok = response.status_code < 400
        if not ok:
            logger.warning("trade notifier failed | status=%s body=%s", response.status_code, response.text[:200])
        return ok
    except Exception as exc:
        logger.warning("trade notifier exception: %s", exc)
        return False
