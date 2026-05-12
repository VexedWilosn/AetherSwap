import sys
import threading
import webbrowser
from pathlib import Path
if sys.platform == "win32":
    import os
    os.chdir(Path(__file__).resolve().parent.parent)
try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
import uvicorn
HOST = "127.0.0.1"
PORT = 28472
URL = f"http://{HOST}:{PORT}"
def run_server():
    uvicorn.run(
        "app.api:app",
        host=HOST,
        port=PORT,
        log_level="warning",
    )
def main():
    import time
    
    disclaimer_file = Path(".agreed_disclaimer")
    if not disclaimer_file.exists():
        print("本程序仅供学习，运行即代表同意 README 中的免责声明。")
        confirm = input("是否继续？(y/n): ")
        if confirm.strip().lower() != 'y':
            sys.exit(0)
        try:
            disclaimer_file.touch()
        except Exception:
            pass

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1.2)
    if HAS_WEBVIEW:
        storage_path = str((Path("config") / "webview_profile").resolve())
        webview.create_window("aetherswap", URL, width=1280, height=800, zoomable=True, maximized=True)
        webview.start(private_mode=False, storage_path=storage_path)
        print(f"窗口已关闭，后端仍在运行。在浏览器打开 {URL} 可继续查看状态。按 Ctrl+C 退出。")
        try:
            while t.is_alive():
                t.join(timeout=1)
        except KeyboardInterrupt:
            pass
    else:
        webbrowser.open(URL)
        try:
            while t.is_alive():
                t.join(timeout=1)
        except KeyboardInterrupt:
            pass
if __name__ == "__main__":
    main()
