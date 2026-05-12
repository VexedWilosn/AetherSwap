from __future__ import annotations

import asyncio
import json

from curl_cffi.requests import AsyncSession

from utils.proxy_manager import get_proxy_manager

TEST_URL = "https://api.ipify.org?format=json"


async def probe_proxy(proxies: dict[str, str] | None) -> bool:
    async with AsyncSession(impersonate="chrome120") as session:
        response = await session.get(TEST_URL, proxies=proxies, timeout=20)
        print("status:", response.status_code)
        print("body:", response.text[:300])
        response.raise_for_status()
        data = response.json()
        print("ip:", data.get("ip"))
        return bool(data.get("ip"))


async def main() -> int:
    manager = get_proxy_manager()
    configs = list(getattr(manager, "_proxy_configs", []) or [])
    if not configs:
        print("No proxies configured in config/app_config.json.")
        return 2

    for index, cfg in enumerate(configs, start=1):
        host = cfg.get("host")
        port = cfg.get("port")
        username = cfg.get("username") or ""
        password = cfg.get("password") or ""
        if username and password:
            url = f"http://{username}:{password}@{host}:{port}"
        else:
            url = f"http://{host}:{port}"
        proxies = {"http": url, "https": url}
        safe_cfg = {**cfg, "username": "***" if username else "", "password": "***" if password else ""}
        print(f"\n[{index}/{len(configs)}] testing proxy:", json.dumps(safe_cfg, ensure_ascii=False))
        try:
            if await probe_proxy(proxies):
                print("PROXY_OK:", host, port)
                return 0
        except Exception as exc:
            print("PROXY_FAILED:", type(exc).__name__, exc)

    print("\nAll configured proxies failed.")
    print("请检查 config 中的代理账号密码是否正确，或登录代理提供商后台添加本机 IP 白名单。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
