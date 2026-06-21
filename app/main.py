from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app.runtime_env import (
    env_bool,
    get_bind_host,
    get_external_url_hint,
    get_local_url,
    get_port,
    get_runtime_profile,
)


if sys.platform == "win32":
    os.chdir(Path(__file__).resolve().parent.parent)


def check_runtime_dependencies() -> None:
    try:
        import typing_extensions as te
    except Exception as exc:
        print(f"依赖检查失败: 无法导入 typing_extensions: {exc}")
        print("请运行: python -m pip install -r requirements.txt")
        sys.exit(1)
    missing = [name for name in ("Sentinel", "TypeAliasType") if not hasattr(te, name)]
    if missing:
        print("依赖版本过旧: typing_extensions 缺少 " + ", ".join(missing))
        print("FastAPI/Pydantic/SQLAlchemy 需要新版 typing_extensions。")
        print("请在项目目录运行: python -m pip install --upgrade typing_extensions")
        print("或运行: python -m pip install -r requirements.txt")
        sys.exit(1)


def _ensure_disclaimer() -> None:
    disclaimer_file = Path(".agreed_disclaimer")
    if disclaimer_file.exists():
        return
    if env_bool("AETHERSWAP_AGREE_DISCLAIMER", False):
        try:
            disclaimer_file.touch()
        except Exception:
            pass
        return
    if not sys.stdin.isatty():
        print("首次运行需要确认 README 中的免责声明。")
        print("交互式运行 python run.py 后输入 y，或确认已阅读后设置 AETHERSWAP_AGREE_DISCLAIMER=1。")
        sys.exit(1)
    print("本程序仅供学习，运行即代表同意 README 中的免责声明。")
    confirm = input("是否继续？(y/n): ")
    if confirm.strip().lower() != "y":
        sys.exit(0)
    try:
        disclaimer_file.touch()
    except Exception:
        pass


def _load_webview():
    try:
        import webview
    except Exception as exc:
        return None, exc
    return webview, None


def __getattr__(name: str):
    if name == "app":
        from app.api import app as api_app
        return api_app
    raise AttributeError(name)


def run_server(host: str | None = None, port: int | None = None) -> None:
    profile = get_runtime_profile()
    uvicorn.run(
        "app.api:app",
        host=host or get_bind_host(profile),
        port=port or get_port(),
        log_level="warning",
    )


def _wait_for_server_thread(thread: threading.Thread) -> None:
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    check_runtime_dependencies()
    _ensure_disclaimer()

    profile = get_runtime_profile()
    host = get_bind_host(profile)
    port = get_port()
    local_url = get_local_url(host, port)
    external_hint = get_external_url_hint(host, port)

    print(f"AetherSwap runtime: {profile.mode} ({profile.reason})")
    print(f"Listening on {external_hint}")

    if profile.mode == "server":
        print("未检测到可用桌面环境，已进入服务器模式。请使用外部浏览器访问上面的地址。")
        run_server(host, port)
        return
    if not profile.open_browser:
        print(f"已按配置禁用自动打开浏览器，请手动访问 {local_url}。")
        run_server(host, port)
        return

    thread = threading.Thread(target=run_server, args=(host, port), daemon=True)
    thread.start()
    time.sleep(1.2)

    webview, webview_error = _load_webview()
    if webview:
        try:
            webview.create_window("aetherswap", local_url, width=1280, height=800, zoomable=True, maximized=True)
            webview.start()
            print(f"窗口已关闭，后端仍在运行。浏览器打开 {local_url} 可继续查看状态。按 Ctrl+C 退出。")
            _wait_for_server_thread(thread)
            return
        except Exception as exc:
            print(f"桌面窗口启动失败，改用系统浏览器: {exc}")
    elif webview_error:
        print(f"未启用内嵌桌面窗口，改用系统浏览器: {webview_error}")

    try:
        webbrowser.open(local_url)
    except Exception as exc:
        print(f"系统浏览器打开失败，请手动访问 {local_url}: {exc}")
    _wait_for_server_thread(thread)


if __name__ == "__main__":
    main()
