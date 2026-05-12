from __future__ import annotations

import logging
from urllib.parse import urlparse

from DataEngine.proxy_observer import proxy_tag

logger = logging.getLogger(__name__)
_warned_local_single_proxy = False


def warmup_proxy_pool() -> None:
    """Start a best-effort proxy health check so bad nodes are not picked first."""
    try:
        from utils.proxy_manager import get_proxy_manager

        manager = get_proxy_manager()
        if not manager.is_proxy_enabled():
            logger.info("proxy warmup skipped: proxy pool disabled")
            return
        manager.warmup()
    except Exception as exc:
        logger.debug("proxy warmup skipped | err=%s", exc)


def get_request_proxies(*, failed: bool = False, force: bool = False, platform: str | None = None) -> dict | None:
    """Return a per-request proxy from the configured AetherSwap proxy pool."""
    try:
        from utils.proxy_manager import get_proxy_manager

        manager = get_proxy_manager()
        proxies = (
            manager.get_next_proxy_dict(platform=platform)
            if force
            else manager.get_proxies_for_request(failed=failed, platform=platform)
        )
        _warn_if_single_local_proxy(manager, proxies)
        return proxies
    except Exception as exc:
        logger.debug("proxy pool unavailable, using direct connection | err=%s", exc)
        return None


def proxy_url_from_dict(proxies: dict | None) -> str | None:
    if not proxies:
        return None
    return proxies.get("https") or proxies.get("http")


def mark_proxy_failure(proxies: dict | str | None, reason: str = "", cooldown_seconds: int = 180) -> bool:
    proxy_url = proxy_url_from_dict(proxies) if isinstance(proxies, dict) else proxies
    if not proxy_url:
        return False
    try:
        from utils.proxy_manager import get_proxy_manager

        return get_proxy_manager().mark_proxy_failure(proxy_url, reason=reason, cooldown_seconds=cooldown_seconds)
    except Exception as exc:
        logger.debug("proxy failure report ignored | proxy=%s err=%s", proxy_log_tag(proxies), exc)
        return False


def mark_proxy_success(proxies: dict | str | None) -> bool:
    proxy_url = proxy_url_from_dict(proxies) if isinstance(proxies, dict) else proxies
    if not proxy_url:
        return False
    try:
        from utils.proxy_manager import get_proxy_manager

        return get_proxy_manager().mark_proxy_success(proxy_url)
    except Exception as exc:
        logger.debug("proxy success report ignored | proxy=%s err=%s", proxy_log_tag(proxies), exc)
        return False


def classify_request_failure(exc: object = None, *, status_code: int | None = None) -> str:
    if status_code == 407 or (exc is not None and is_proxy_auth_error(exc)):
        return "proxy_auth"
    if status_code in {401, 403}:
        return "blocked"
    if status_code == 429:
        return "rate_limited"
    text = str(exc or "").lower()
    if "connection was reset" in text or "recv failure" in text or "reset" in text:
        return "connection_reset"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "stop requested" in text:
        return "stop_requested"
    return "network"


def proxy_cooldown_for_reason(reason: str, *, default: int = 180) -> int:
    return {
        "proxy_auth": 900,
        "blocked": 600,
        "rate_limited": 300,
        "connection_reset": 180,
        "timeout": 180,
    }.get(reason, default)


def is_proxy_auth_error(exc: object) -> bool:
    text = str(exc).lower()
    return "407" in text or "proxy authentication" in text or "connect tunnel failed" in text


def proxy_log_tag(proxies: dict | str | None) -> str:
    if isinstance(proxies, dict):
        return proxy_tag(proxy_url_from_dict(proxies))
    return proxy_tag(proxies)


def _warn_if_single_local_proxy(manager, proxies: dict | None) -> None:
    global _warned_local_single_proxy
    if _warned_local_single_proxy or not proxies:
        return
    try:
        configs = list(getattr(manager, "_proxy_configs", []) or [])
        if len(configs) != 1:
            return
        host = str(configs[0].get("host") or "")
        proxy_url = proxy_url_from_dict(proxies) or ""
        parsed = urlparse(proxy_url)
        if host in {"127.0.0.1", "localhost", "::1"} or parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            logger.warning(
                "proxy pool has a single local proxy %s; enable Clash/v2ray load-balance group for real node distribution",
                proxy_log_tag(proxies),
            )
            _warned_local_single_proxy = True
    except Exception:
        return
