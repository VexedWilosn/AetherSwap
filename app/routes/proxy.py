"""代理池配置与测试路由."""
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel
from app.config_loader import load_app_config_validated, save_app_config_validated
router = APIRouter()
class ProxyEntry(BaseModel):
    host: str
    port: int
    username: str = ""
    password: str = ""
class ProxyPoolConfig(BaseModel):
    enabled: bool = False
    strategy: int = 1
    test_url: str = "https://ipv4.webshare.io/"
    timeout_seconds: int = 10
    webshare_api_key: str = ""
    global_proxies: List[str] = []
    steam_proxies: List[str] = []
    buff_proxies: List[str] = []
    uuyp_proxies: List[str] = []
    proxies: List[ProxyEntry] = []
class ProxyPoolBody(BaseModel):
    proxy_pool: ProxyPoolConfig
@router.get("/api/proxy/config")
def api_get_proxy_config():
    cfg = load_app_config_validated()
    pool = dict(cfg.get("proxy_pool", {}) or {})
    if not pool.get("global_proxies") and pool.get("proxies"):
        pool["global_proxies"] = [_proxy_entry_to_line(p) for p in pool.get("proxies", []) if p.get("host")]
    return {"proxy_pool": pool}
@router.get("/api/proxy/status")
def api_proxy_status():
    try:
        from utils.proxy_manager import get_proxy_manager
        return {"ok": True, "proxy_pool": get_proxy_manager().status_snapshot()}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "proxy_pool": {"nodes": []}}
@router.post("/api/proxy/config")
def api_save_proxy_config(body: ProxyPoolBody):
    cfg = load_app_config_validated()
    new_pool = body.proxy_pool.dict()
    if not new_pool.get("global_proxies") and new_pool.get("proxies"):
        new_pool["global_proxies"] = [_proxy_entry_to_line(p) for p in new_pool.get("proxies", []) if p.get("host")]
    cfg["proxy_pool"] = new_pool
    save_app_config_validated(cfg)
    try:
        from utils.proxy_manager import get_proxy_manager
        pm = get_proxy_manager()
        pm.reload()
        from app.state import log
        log(
            f"[proxy] 配置已保存并重载: enabled={new_pool.get('enabled')} "
            f"strategy={new_pool.get('strategy')} "
            f"代理数={len([p for p in new_pool.get('proxies', []) if p.get('host')])}",
            "info",
            category="proxy",
        )
    except Exception as e:
        pass  
    return {"ok": True}

def _proxy_entry_to_line(p: dict) -> str:
    base = f"{p.get('host', '')}:{p.get('port', '')}"
    if p.get("username") or p.get("password"):
        return f"{base}:{p.get('username', '')}:{p.get('password', '')}"
    return base
def _proxy_line_to_entry(line: str) -> dict:
    text = (line or "").strip()
    if not text:
        return {}
    if "://" in text:
        from urllib.parse import urlparse
        parsed = urlparse(text)
        return {
            "host": parsed.hostname or "",
            "port": int(parsed.port or 0),
            "username": parsed.username or "",
            "password": parsed.password or "",
        }
    parts = text.split(":")
    return {
        "host": parts[0].strip() if len(parts) > 0 else "",
        "port": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
        "username": parts[2].strip() if len(parts) > 2 else "",
        "password": parts[3].strip() if len(parts) > 3 else "",
    }
def _proxy_pool_entries(pool_cfg: dict) -> list[dict]:
    pools = [
        ("global", pool_cfg.get("proxies") or [_proxy_line_to_entry(p) for p in pool_cfg.get("global_proxies", [])]),
        ("steam", [_proxy_line_to_entry(p) for p in pool_cfg.get("steam_proxies", [])]),
        ("buff", [_proxy_line_to_entry(p) for p in pool_cfg.get("buff_proxies", [])]),
        ("uuyp", [_proxy_line_to_entry(p) for p in pool_cfg.get("uuyp_proxies", [])]),
    ]
    entries = []
    for pool_name, proxies in pools:
        for proxy in proxies or []:
            if proxy and proxy.get("host"):
                proxy = dict(proxy)
                proxy["pool"] = pool_name
                entries.append(proxy)
    return entries
def _proxy_test_url_for_pool(pool_name: str, fallback_url: str) -> str:
    return {
        "steam": "https://steamcommunity.com/market/",
        "buff": "https://buff.163.com/",
        "uuyp": "https://www.youpin898.com/",
    }.get(pool_name, fallback_url)
@router.post("/api/proxy/test")
def api_test_proxies():
    cfg = load_app_config_validated()
    pool_cfg = cfg.get("proxy_pool", {})
    proxies_list = _proxy_pool_entries(pool_cfg)
    test_url = pool_cfg.get("test_url", "https://ipv4.webshare.io/")
    timeout = int(pool_cfg.get("timeout_seconds", 10))
    from utils.proxy_manager import test_one_proxy
    if not proxies_list:
        return {"results": []}
    results = []
    max_workers = min(len(proxies_list), 20)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(test_one_proxy, p, _proxy_test_url_for_pool(p.get("pool", "global"), test_url), timeout): p
            for p in proxies_list
        }
        for future in as_completed(future_map):
            p = future_map[future]
            try:
                result = future.result()
                result["pool"] = p.get("pool", "global")
                result["test_url"] = _proxy_test_url_for_pool(p.get("pool", "global"), test_url)
                results.append(result)
            except Exception as e:
                results.append({
                    "pool": p.get("pool", "global"),
                    "host": p.get("host", ""),
                    "port": p.get("port", 0),
                    "status": "failed",
                    "ip_detected": None,
                    "latency_ms": 0,
                    "error": str(e),
                    "test_url": _proxy_test_url_for_pool(p.get("pool", "global"), test_url),
                })
    return {"results": results}
@router.post("/api/proxy/clear")
def api_clear_proxies():
    """清空代理池列表并保存."""
    cfg = load_app_config_validated()
    pool = cfg.get("proxy_pool", {})
    pool["proxies"] = []
    pool["global_proxies"] = []
    pool["steam_proxies"] = []
    pool["buff_proxies"] = []
    pool["uuyp_proxies"] = []
    cfg["proxy_pool"] = pool
    save_app_config_validated(cfg)
    try:
        from utils.proxy_manager import get_proxy_manager
        get_proxy_manager().reload()
    except Exception:
        pass
    return {"ok": True, "message": "代理列表已清空"}
@router.post("/api/proxy/webshare")
def api_fetch_webshare():
    """从 Webshare API 拉取代理列表并追加／覆盖到代理池."""
    cfg = load_app_config_validated()
    pool_cfg = cfg.get("proxy_pool", {})
    api_key = pool_cfg.get("webshare_api_key", "").strip()
    if not api_key:
        return {"ok": False, "message": "未配置 Webshare API Key，请先在代理池设置中填写"}
    fetched = []
    for mode in ("direct", "backbone"):
        page = 1
        while True:
            url = f"https://proxy.webshare.io/api/v2/proxy/list/?mode={mode}&page={page}&page_size=100"
            headers = {"Authorization": f"Token {api_key}"}
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 401:
                    return {"ok": False, "message": "API Key 无效或已过期（401 Unauthorized）"}
                if resp.status_code != 200:
                    break  
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                for p in results:
                    fetched.append({
                        "host": p.get("proxy_address", ""),
                        "port": int(p.get("port", 0)),
                        "username": p.get("username", ""),
                        "password": p.get("password", ""),
                    })
                if data.get("next"):
                    page += 1
                else:
                    break
            except Exception as e:
                return {"ok": False, "message": f"请求 Webshare 失败: {str(e)}"}
        if fetched:
            break  
    if not fetched:
        return {"ok": False, "message": "未获取到任何代理，请检查账户或套餐状态"}
    pool_cfg["proxies"] = [p for p in fetched if p["host"]]
    pool_cfg["global_proxies"] = [_proxy_entry_to_line(p) for p in pool_cfg["proxies"]]
    cfg["proxy_pool"] = pool_cfg
    save_app_config_validated(cfg)
    try:
        from utils.proxy_manager import get_proxy_manager
        get_proxy_manager().reload()
    except Exception:
        pass
    return {"ok": True, "count": len(fetched), "message": f"已成功获取并配置 {len(fetched)} 个代理"}
