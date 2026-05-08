import sys
import signal
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
_server = None
_window = None
_shutdown_requested = threading.Event()
_console_ctrl_handler = None


def run_server():
    global _server
    config = uvicorn.Config(
        "app.api:app",
        host=HOST,
        port=PORT,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _server.run()


def stop_server():
    if _server is not None:
        _server.should_exit = True


def request_shutdown():
    _shutdown_requested.set()
    stop_server()
    if _window is not None:
        try:
            _window.destroy()
        except Exception:
            pass


def install_ctrl_c_handler():
    def handle_signal(signum, frame):
        request_shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    if sys.platform != "win32":
        return

    # pywebview blocks the main thread on Windows, so SIGINT is not always
    # delivered until the GUI loop returns. Register a console control handler
    # so Ctrl+C can tear down the GUI loop immediately.
    import ctypes

    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def handle_console_ctrl(ctrl_type):
        if ctrl_type in (
            CTRL_C_EVENT,
            CTRL_BREAK_EVENT,
            CTRL_CLOSE_EVENT,
            CTRL_LOGOFF_EVENT,
            CTRL_SHUTDOWN_EVENT,
        ):
            request_shutdown()
            return True
        return False

    global _console_ctrl_handler
    _console_ctrl_handler = handler_type(handle_console_ctrl)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)


def main():
    global _window
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

    install_ctrl_c_handler()
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1.2)
    if HAS_WEBVIEW:
        _window = webview.create_window("aetherswap", URL, width=1280, height=800, zoomable=True, maximized=True)
        webview.start()
        if _shutdown_requested.is_set():
            t.join(timeout=5)
            print("已退出。")
            return
        print(f"窗口已关闭，后端仍在运行。在浏览器打开 {URL} 可继续查看状态。按 Ctrl+C 退出。")
        while t.is_alive() and not _shutdown_requested.is_set():
            t.join(timeout=1)
        t.join(timeout=5)
        print("已退出。")
    else:
        webbrowser.open(URL)
        print(f"已在浏览器打开 {URL}。按 Ctrl+C 退出。")
        while t.is_alive() and not _shutdown_requested.is_set():
            t.join(timeout=1)
        t.join(timeout=5)
        print("已退出。")


if __name__ == "__main__":
    main()
