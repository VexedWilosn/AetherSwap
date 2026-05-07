import os
import signal
import time
from pathlib import Path
from typing import Callable, Optional


def release_chromium_profile(profile_dir: Path, log_fn: Optional[Callable[[str], None]] = None) -> int:
    """Stop Chromium processes using a persistent profile and remove stale locks.

    This is intended for Docker/noVNC relogin flows where a previous headed
    browser can be left open after the user retries the login step.
    """
    if os.name != "posix":
        return 0

    profile_path = str(Path(profile_dir).resolve())
    matched = []
    proc_dir = Path("/proc")
    for child in proc_dir.iterdir():
        if not child.name.isdigit():
            continue
        pid = int(child.name)
        if pid == os.getpid():
            continue
        try:
            raw = (child / "cmdline").read_bytes()
        except Exception:
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        if profile_path in cmdline and ("chrome" in cmdline or "chromium" in cmdline):
            matched.append(pid)

    for pid in matched:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            if log_fn:
                log_fn(f"终止旧 Chromium 进程失败 pid={pid}: {exc}")

    if matched:
        time.sleep(1.0)

    for pid in matched:
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            if log_fn:
                log_fn(f"强制终止旧 Chromium 进程失败 pid={pid}: {exc}")

    for lock_name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        lock_path = Path(profile_dir) / lock_name
        try:
            if lock_path.exists() or lock_path.is_symlink():
                lock_path.unlink()
        except Exception as exc:
            if log_fn:
                log_fn(f"清理 Chromium profile 锁文件失败 {lock_name}: {exc}")

    return len(matched)
