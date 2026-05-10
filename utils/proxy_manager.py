"""
浠ｇ悊姹犵鐞嗗櫒 鈥?渚?pipeline / steam 璇锋眰妯″潡璋冪敤銆?
绛栫暐璇存槑:
  1 = 鏈満浼樺厛锛氭湰鏈鸿姹傚け璐?瓒呮椂鍚庡垏鎹唬鐞嗛噸璇?
  2 = 瀹屽叏璧颁唬鐞嗘睜
  3 = 鍏抽棴浠ｇ悊锛堝彧璧版湰鏈猴級
"""
import random
import threading
import time
import requests
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROXY_AUTH_COOLDOWN_SECONDS = 180
BASE_DIR = Path(__file__).resolve().parent.parent
PROXY_HEALTH_STATE_PATH = BASE_DIR / "config" / "proxy_health_state.json"
BRIGHT_DATA_HOSTS = {"brd.superproxy.io", "zproxy.lum-superproxy.io"}
BRIGHT_DATA_TEST_URL = "https://geo.brdtest.com/welcome.txt?product=dc&method=native"


def _proxy_identity(host: str, port: int, username: str = "", password: str = "") -> str:
    secret_hash = hashlib.sha256(str(password or "").encode("utf-8")).hexdigest()[:12] if password else ""
    return f"{str(host or '').lower()}:{int(port or 0)}:{username or ''}:{secret_hash}"
def _proxy_legacy_state_key(config: dict) -> str:
    return f"{config.get('host')}:{config.get('port')}"
def _proxy_key_from_url(proxy_url: str) -> str | None:
    try:
        parsed = urlparse(proxy_url or "")
        if not parsed.hostname or not parsed.port:
            return None
        return _proxy_identity(parsed.hostname, int(parsed.port), parsed.username or "", parsed.password or "")
    except Exception:
        return None
def _load_proxy_pool_cfg() -> dict:
    try:
        from app.config_loader import load_app_config_validated
        cfg = load_app_config_validated()
        return cfg.get("proxy_pool", {})
    except Exception:
        return {}
def _build_proxy_url(p: dict) -> str:
    host = p.get("host", "")
    port = p.get("port", 0)
    user = p.get("username", "")
    pwd = p.get("password", "")
    if user and pwd:
        return f"http://{user}:{pwd}@{host}:{port}/"
    return f"http://{host}:{port}/"
def _parse_proxy_line(line: str) -> Optional[dict]:
    text = (line or "").strip()
    if not text:
        return None
    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname or not parsed.port:
            return None
        return {
            "host": parsed.hostname,
            "port": int(parsed.port),
            "username": parsed.username or "",
            "password": parsed.password or "",
        }
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        port = int(parts[1])
    except (TypeError, ValueError):
        return None
    return {
        "host": parts[0].strip(),
        "port": port,
        "username": parts[2].strip() if len(parts) > 2 else "",
        "password": parts[3].strip() if len(parts) > 3 else "",
    }
def _normalize_proxy_entry(entry) -> Optional[dict]:
    if isinstance(entry, str):
        return _parse_proxy_line(entry)
    if isinstance(entry, dict):
        try:
            host = str(entry.get("host") or "").strip()
            port = int(entry.get("port") or 0)
        except (TypeError, ValueError):
            return None
        if not host or not port:
            return None
        return {
            "host": host,
            "port": port,
            "username": str(entry.get("username") or ""),
            "password": str(entry.get("password") or ""),
        }
    return None
def _proxy_state_key(config: dict) -> str:
    return _proxy_identity(
        config.get("host", ""),
        int(config.get("port") or 0),
        str(config.get("username") or ""),
        str(config.get("password") or ""),
    )
def _is_bright_data_proxy(proxy_cfg: dict) -> bool:
    return str(proxy_cfg.get("host") or "").lower() in BRIGHT_DATA_HOSTS
def _pm_log(msg: str) -> None:
    try:
        from app.state import log as app_log
        app_log(msg, "debug", category="proxy")
    except Exception:
        pass
def test_one_proxy(proxy_cfg: dict, test_url: str, timeout: int) -> dict:
    proxy_url = _build_proxy_url(proxy_cfg)
    proxies = {"http": proxy_url, "https": proxy_url}
    result = {
        "host": proxy_cfg.get("host", ""),
        "port": proxy_cfg.get("port", 0),
        "username": proxy_cfg.get("username", ""),
        "status": "failed",
        "proxy_status": "unknown",
        "target_status": "failed",
        "ip_detected": None,
        "latency_ms": 0,
        "proxy_latency_ms": 0,
        "error": None,
        "proxy_error": None,
        "target_error": None,
    }

    if _is_bright_data_proxy(proxy_cfg):
        start = time.time()
        try:
            resp = requests.get(BRIGHT_DATA_TEST_URL, proxies=proxies, timeout=timeout)
            result["proxy_latency_ms"] = round((time.time() - start) * 1000, 1)
            if resp.status_code == 200:
                result["proxy_status"] = "ok"
            else:
                result["proxy_status"] = "failed"
                result["proxy_error"] = f"gateway_http_{resp.status_code}"
        except requests.exceptions.ProxyError:
            result["proxy_status"] = "failed"
            result["proxy_error"] = "proxy_error"
        except requests.exceptions.Timeout:
            result["proxy_status"] = "failed"
            result["proxy_error"] = "gateway_timeout"
        except Exception as e:
            result["proxy_status"] = "failed"
            result["proxy_error"] = str(e)

    start = time.time()
    try:
        resp = requests.get(test_url, proxies=proxies, timeout=timeout)
        result["latency_ms"] = round((time.time() - start) * 1000, 1)
        if resp.status_code == 200:
            result["status"] = "ok"
            result["target_status"] = "ok"
            if result["proxy_status"] == "unknown":
                result["proxy_status"] = "ok"
            result["ip_detected"] = resp.text.strip()
        else:
            result["error"] = f"HTTP {resp.status_code}"
            result["target_error"] = result["error"]
    except requests.exceptions.ProxyError:
        result["error"] = "proxy_error"
        result["target_error"] = result["error"]
        if result["proxy_status"] == "unknown":
            result["proxy_status"] = "failed"
            result["proxy_error"] = result["error"]
    except requests.exceptions.Timeout:
        result["error"] = "target_timeout"
        result["target_error"] = result["error"]
    except Exception as e:
        result["error"] = str(e)
        result["target_error"] = result["error"]

    if result["target_status"] != "ok" and result["proxy_status"] == "ok":
        result["status"] = "target_failed"
    elif result["target_status"] != "ok":
        result["status"] = "failed"
    return result
class ProxyManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._proxy_configs: list = []
        self._proxy_weights: list = []
        self._proxy_group_configs: dict[str, list] = {}
        self._proxy_group_weights: dict[str, list] = {}
        self._proxies: list = []
        self._warming_up: bool = False
        self._reload()
        _pm_log(
            f"[ProxyManager] 鍒濆鍖栧畬鎴? "
            f"浠ｇ悊鏁?{len(self._proxies)} "
            f"宸插惎鐢?{self.is_proxy_enabled()} "
            f"绛栫暐={self.get_strategy()}"
        )
    def _reload(self):
        cfg = _load_proxy_pool_cfg()
        raw_by_pool = {
            "global": cfg.get("global_proxies") or cfg.get("proxies", []),
            "steam": cfg.get("steam_proxies", []),
            "buff": cfg.get("buff_proxies", []),
            "uuyp": cfg.get("uuyp_proxies", []),
        }
        state = self._load_health_state()
        self._proxies = []
        now = time.time()
        for pool_name, raw in raw_by_pool.items():
            for entry in raw or []:
                p = _normalize_proxy_entry(entry)
                if not p:
                    continue
                key = _proxy_state_key(p)
                health = state.get(key, {}) if isinstance(state, dict) else {}
                cooldown_until = float(health.get("cooldown_until") or 0)
                score = max(0, int(health.get("score", 1) or 0))
                if cooldown_until <= now:
                    cooldown_until = 0.0
                    score = max(1, score)
                self._proxies.append(
                    {
                        "config": p,
                        "pool": pool_name,
                        "score": score,
                        "cooldown_until": cooldown_until,
                        "failures": int(health.get("failures", 0) or 0),
                        "successes": int(health.get("successes", 0) or 0),
                        "health": dict(health),
                    }
                )
        self._cached_strategy = int(cfg.get("strategy", 1))
        self._cached_enabled = bool(cfg.get("enabled", False))
        self._sync_cycle()
    def _load_health_state(self) -> dict:
        try:
            if not PROXY_HEALTH_STATE_PATH.exists():
                return {}
            data = json.loads(PROXY_HEALTH_STATE_PATH.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            _pm_log(f"[ProxyManager] health state load failed: {exc}")
            return {}
    def _save_health_state_locked(self) -> None:
        try:
            PROXY_HEALTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            state = {}
            for proxy in self._proxies:
                cfg = proxy.get("config", {})
                key = _proxy_state_key(cfg)
                health = dict(proxy.get("health") or {})
                health.update(
                    {
                        "score": int(proxy.get("score") or 0),
                        "cooldown_until": float(proxy.get("cooldown_until") or 0),
                        "failures": int(proxy.get("failures") or 0),
                        "successes": int(proxy.get("successes") or 0),
                    }
                )
                state[key] = health
                state.setdefault(_proxy_legacy_state_key(cfg), dict(health))
            PROXY_HEALTH_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            _pm_log(f"[ProxyManager] health state save failed: {exc}")
    def _sync_cycle(self):
        now = time.time()
        self._proxies.sort(key=lambda x: x["score"], reverse=True)
        # 鎸夎瘎鍒嗘瀯寤烘潈閲嶅垪琛紝score=0鐨勮妭鐐规潈閲嶄负0锛屾案杩滀笉琚€変腑
        active = [
            p
            for p in self._proxies
            if float(p.get("cooldown_until") or 0) <= now and int(p.get("score") or 0) > 0
        ]
        self._proxy_group_configs = {}
        self._proxy_group_weights = {}
        for p in active:
            pool = p.get("pool") or "global"
            self._proxy_group_configs.setdefault(pool, []).append(p["config"])
            self._proxy_group_weights.setdefault(pool, []).append(max(1, int(p.get("score") or 0)))
        self._proxy_configs = self._proxy_group_configs.get("global", [])
        self._proxy_weights = self._proxy_group_weights.get("global", [])
    def reload(self):
        with self._lock:
            self._reload()
        _pm_log(
            f"[ProxyManager] reload 瀹屾垚: "
            f"浠ｇ悊鏁?{len(self._proxies)} "
            f"宸插惎鐢?{self.is_proxy_enabled()} "
            f"绛栫暐={self.get_strategy()}"
        )
        threading.Thread(target=self.warmup, daemon=True).start()
    def warmup(self):
        with self._lock:
            if getattr(self, "_warming_up", False):
                return
            self._warming_up = True
            proxies_snapshot = list(self._proxies)
        if not proxies_snapshot:
            with self._lock:
                self._warming_up = False
            return
        _pm_log(f"[ProxyManager] 寮€濮嬮鐑拰妫€娴嬩唬鐞嗘睜锛屽叡 {len(proxies_snapshot)} 涓唬鐞?..")
        cfg = _load_proxy_pool_cfg()
        test_url = cfg.get("test_url", "https://ipv4.webshare.io/")
        timeout = int(cfg.get("timeout_seconds", 10))
        results_map = {}
        max_workers = min(len(proxies_snapshot), 20)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_proxy = {
                executor.submit(test_one_proxy, p["config"], test_url, timeout): p["config"]
                for p in proxies_snapshot
            }
            for future in as_completed(future_to_proxy):
                config = future_to_proxy[future]
                key = _proxy_state_key(config)
                try:
                    res = future.result()
                    if res["status"] == "ok":
                        score = max(1, 100000 - res["latency_ms"])
                    else:
                        score = 0
                except Exception:
                    score = 0
                results_map[key] = score
        with self._lock:
            for p in self._proxies:
                key = _proxy_state_key(p["config"])
                if key in results_map:
                    p["score"] = results_map[key]
                    if results_map[key] > 0:
                        p["cooldown_until"] = 0.0
                        p["failures"] = 0
                        p["successes"] = int(p.get("successes") or 0) + 1
                        health = p.setdefault("health", {})
                        health["last_success_at"] = time.time()
                        health["last_latency_score"] = results_map[key]
            self._sync_cycle()
            self._save_health_state_locked()
            self._warming_up = False
        _pm_log("[ProxyManager] warmup completed")
    def get_strategy(self) -> int:
        return self._cached_strategy
    def is_proxy_enabled(self) -> bool:
        return self._cached_enabled and self._cached_strategy != 3
    def status_snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            nodes = []
            active_count = 0
            cooldown_count = 0
            for proxy in self._proxies:
                cfg = dict(proxy.get("config") or {})
                cooldown_until = float(proxy.get("cooldown_until") or 0)
                score = int(proxy.get("score") or 0)
                is_cooldown = cooldown_until > now
                if is_cooldown:
                    state = "cooldown"
                    cooldown_count += 1
                elif score > 0:
                    state = "active"
                    active_count += 1
                else:
                    state = "unavailable"
                health = dict(proxy.get("health") or {})
                nodes.append(
                    {
                        "host": cfg.get("host", ""),
                        "port": cfg.get("port", 0),
                        "username": cfg.get("username", ""),
                        "state": state,
                        "score": score,
                        "cooldown_until": cooldown_until,
                        "cooldown_remaining": max(0, round(cooldown_until - now)),
                        "failures": int(proxy.get("failures") or 0),
                        "successes": int(proxy.get("successes") or 0),
                        "last_failure_reason": health.get("last_failure_reason", ""),
                        "last_success_at": health.get("last_success_at", 0),
                        "last_failure_at": health.get("last_failure_at", 0),
                    }
                )
            return {
                "enabled": self.is_proxy_enabled(),
                "strategy": self.get_strategy(),
                "total": len(self._proxies),
                "active": active_count,
                "cooldown": cooldown_count,
                "unavailable": max(0, len(self._proxies) - active_count - cooldown_count),
                "nodes": nodes,
            }
    def get_next_proxy_dict(self, platform: Optional[str] = None):
        with self._lock:
            platform_key = (platform or "").strip().lower()
            configs = self._proxy_group_configs.get(platform_key, []) if platform_key else []
            weights = self._proxy_group_weights.get(platform_key, []) if platform_key else []
            source = platform_key
            if not configs or not weights or sum(weights) <= 0:
                configs = self._proxy_configs
                weights = self._proxy_weights
                source = "global"
            if not configs or not weights or sum(weights) <= 0:
                _pm_log("[ProxyManager] get_next_proxy_dict 鈫?浠ｇ悊姹犱负绌烘垨鍏ㄩ儴澶辫触锛岃繑鍥?None")
                return None
            # 鎸夎瘎鍒嗗姞鏉冮殢鏈洪€夛細寤惰繜浣庯紙楂樺垎锛夌殑浠ｇ悊琚€変腑姒傜巼鏇撮珮
            p = random.choices(configs, weights=weights, k=1)[0]
            url = _build_proxy_url(p)
            result = {"http": url, "https": url}
            _pm_log(f"[ProxyManager] 鍔犳潈闅忔満閫夊埌浠ｇ悊: {p.get('host')}:{p.get('port')}")
            return result
    def mark_proxy_failure(self, proxy_url: str, reason: str = "", cooldown_seconds: int = PROXY_AUTH_COOLDOWN_SECONDS) -> bool:
        key = _proxy_key_from_url(proxy_url)
        if not key:
            return False
        now = time.time()
        matched = False
        with self._lock:
            for proxy in self._proxies:
                cfg = proxy.get("config", {})
                cfg_key = _proxy_state_key(cfg)
                if cfg_key == key:
                    proxy["failures"] = int(proxy.get("failures") or 0) + 1
                    proxy["score"] = 0
                    proxy["cooldown_until"] = now + max(1, int(cooldown_seconds))
                    health = proxy.setdefault("health", {})
                    health["last_failure_at"] = now
                    health["last_failure_reason"] = reason or "failure"
                    health[f"{reason or 'failure'}_count"] = int(health.get(f"{reason or 'failure'}_count") or 0) + 1
                    matched = True
            if matched:
                self._sync_cycle()
                self._save_health_state_locked()
        if matched:
            _pm_log(
                f"[ProxyManager] proxy cooled down: {key} "
                f"cooldown={cooldown_seconds}s reason={reason or 'failure'}"
            )
        return matched
    def mark_proxy_success(self, proxy_url: str) -> bool:
        key = _proxy_key_from_url(proxy_url)
        if not key:
            return False
        now = time.time()
        matched = False
        with self._lock:
            for proxy in self._proxies:
                cfg = proxy.get("config", {})
                cfg_key = _proxy_state_key(cfg)
                if cfg_key == key:
                    proxy["successes"] = int(proxy.get("successes") or 0) + 1
                    proxy["failures"] = 0
                    proxy["score"] = max(1, int(proxy.get("score") or 1) + 1)
                    proxy["cooldown_until"] = 0.0
                    health = proxy.setdefault("health", {})
                    health["last_success_at"] = now
                    matched = True
            if matched:
                self._sync_cycle()
                self._save_health_state_locked()
        return matched
    def should_use_proxy_on_failure(self) -> bool:
        """Strategy 1: use proxy only after a direct request fails."""
        return self.is_proxy_enabled() and self.get_strategy() == 1
    def should_always_use_proxy(self) -> bool:
        """Strategy 2: always use proxy."""
        return self.is_proxy_enabled() and self.get_strategy() == 2
    def get_proxies_for_request(self, failed: bool = False, platform: Optional[str] = None) -> Optional[dict]:
        """Return a requests proxies dict according to the configured strategy."""
        enabled = self.is_proxy_enabled()
        strategy = self.get_strategy()
        proxy_count = len(self._proxies)
        if not enabled:
            _pm_log(
                f"[ProxyManager] get_proxies_for_request(failed={failed}): "
                f"proxy disabled enabled={enabled}, strategy={strategy}, count={proxy_count}; using direct"
            )
            return None
        if strategy == 3:
            _pm_log(f"[ProxyManager] get_proxies_for_request(failed={failed}): strategy 3 disabled; using direct")
            return None
        if strategy == 2:
            proxy = self.get_next_proxy_dict(platform=platform)
            _pm_log(
                f"[ProxyManager] get_proxies_for_request(failed={failed}): "
                f"strategy 2 always proxy -> {proxy.get('http') if proxy else 'None(empty pool)'}"
            )
            return proxy
        if strategy == 1:
            if failed:
                proxy = self.get_next_proxy_dict(platform=platform)
                _pm_log(
                    f"[ProxyManager] get_proxies_for_request(failed=True): "
                    f"strategy 1 fallback proxy -> {proxy.get('http') if proxy else 'None(empty pool)'}"
                )
                return proxy
            return None
        return None
_manager: Optional[ProxyManager] = None
_manager_lock = threading.Lock()
def get_proxy_manager() -> ProxyManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ProxyManager()
    return _manager
