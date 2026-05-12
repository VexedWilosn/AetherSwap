#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any


IGNORE_DIR_NAMES = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".codex-tmp",
    ".gstack",
    "__pycache__",
    "logs",
    "tmp",
    "node_modules",
}

IGNORE_FILE_PATTERNS = (
    "*.log",
    "*.pid",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.tmp",
    "*.bak",
    "tmp_*.json",
    "tmp_*.js",
    "*.rar",
)

SENSITIVE_KEY_RE = re.compile(
    r"(pass(word)?|secret|token|cookie|api[_-]?key|authorization|webhook|session_?id|steamloginsecure|private_key|app_key|device_id|csrf)",
    re.IGNORECASE,
)

REMOVE_GLOB_PATTERNS = (
    ".codex-tmp",
    ".gstack",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "logs",
    "tmp",
    "**/__pycache__",
    "config/http_cache",
    "config/playwright_*",
    "config/*.db",
    "config/*.db-shm",
    "config/*.db-wal",
    "config/*.sqlite",
    "config/*.sqlite3",
    "DataEngine/*.txt",
    "DataEngine/*.png",
    "DataEngine/uuyp_headers.json",
    "analysis/qa_*",
    "analysis/ship_artifacts_*",
    "analysis/screenshots",
    ".agreed_disclaimer",
    "tmp_*.json",
    "tmp_*.js",
    "*.log",
    "*.pid",
)

STATE_FILES_TO_RESET = (
    "config/session_capsules.json",
    "config/platform_session_state.json",
    "config/platform_runtime_state.json",
    "config/proxy_health_state.json",
    "config/steamdt_openapi_price_quota.json",
    "config/steamdt_openapi_price_checkpoints.json",
    "config/steamdt_openapi_price_state.json",
    "config/steamdt_session_state.json",
    "config/steamdt_waf_cooldown.json",
)


def _blank_like(value: Any) -> Any:
    if isinstance(value, str):
        return ""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return None


def _scrub_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str):
                out[key_str] = _blank_like(value)
            else:
                out[key_str] = _scrub_sensitive(value)
        return out
    if isinstance(obj, list):
        return [_scrub_sensitive(item) for item in obj]
    return obj


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_remove(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return
    path.unlink(missing_ok=True)


def _build_ignore(source_root: Path):
    def _ignore(_dirpath: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in IGNORE_DIR_NAMES:
                ignored.add(name)
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in IGNORE_FILE_PATTERNS):
                ignored.add(name)
        return ignored

    return _ignore


def _sanitize_app_config(path: Path) -> None:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        return

    payload = _scrub_sensitive(payload)

    proxy_pool = payload.get("proxy_pool")
    if isinstance(proxy_pool, dict):
        proxy_pool["enabled"] = False
        for key in ("proxies", "global_proxies", "steam_proxies", "buff_proxies", "uuyp_proxies"):
            proxy_pool[key] = []
        proxy_pool["webshare_api_key"] = ""

    steam_confirm = payload.get("steam_confirm")
    if isinstance(steam_confirm, dict):
        steam_confirm["enabled"] = False
        steam_confirm["identity_secret"] = ""
        steam_confirm["device_id"] = ""

    steam_guard = payload.get("steam_guard")
    if isinstance(steam_guard, dict):
        steam_guard["shared_secret"] = ""

    notify = payload.get("notify")
    if isinstance(notify, dict):
        notify["pushplus_token"] = ""
        notify["email_user"] = ""
        notify["email_pass"] = ""
        notify["target_sender"] = ""
        notify["allowed_sender"] = ""

    steamdt = payload.get("steamdt")
    if isinstance(steamdt, dict):
        steamdt["device_id"] = ""
        steamdt["cookie"] = ""
        openapi = steamdt.get("openapi")
        if isinstance(openapi, dict):
            openapi["api_key"] = ""
        openapi_price = steamdt.get("openapi_price")
        if isinstance(openapi_price, dict):
            openapi_price["api_key"] = ""

    _write_json(path, payload)


def _sanitize_credentials(path: Path) -> None:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        _write_json(path, {})
        return
    _write_json(path, _scrub_sensitive(payload))


def _sanitize_accounts(path: Path) -> None:
    _write_json(path, {"accounts": [], "current_id": None})


def _sanitize_misc_config(path: Path) -> None:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("targets", [])
    notify = payload.get("notify")
    if not isinstance(notify, dict):
        notify = {}
    notify["enabled"] = False
    notify["webhook_url"] = ""
    payload["notify"] = notify
    _write_json(path, payload)


def _reset_state_files(dest_root: Path) -> None:
    for rel in STATE_FILES_TO_RESET:
        state_path = dest_root / rel
        if state_path.exists():
            _write_json(state_path, {})


def _clean_runtime_artifacts(dest_root: Path) -> None:
    for pattern in REMOVE_GLOB_PATTERNS:
        for path in dest_root.glob(pattern):
            _safe_remove(path)

    for path in list(dest_root.rglob("__pycache__")):
        _safe_remove(path)


def build_clean_env(source_root: Path, output_root: Path, with_zip: bool) -> tuple[Path, Path | None]:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_root = output_root / f"{source_root.name}_clean_test_env_{timestamp}"

    if dest_root.exists():
        raise FileExistsError(f"destination already exists: {dest_root}")

    shutil.copytree(
        source_root,
        dest_root,
        ignore=_build_ignore(source_root),
    )

    _clean_runtime_artifacts(dest_root)

    _sanitize_app_config(dest_root / "config" / "app_config.json")
    _sanitize_credentials(dest_root / "config" / "credentials.json")
    _sanitize_accounts(dest_root / "config" / "accounts.json")
    _sanitize_misc_config(dest_root / "config" / "config.json")
    _reset_state_files(dest_root)

    zip_path: Path | None = None
    if with_zip:
        archive_base = str(dest_root)
        zip_file = shutil.make_archive(archive_base, "zip", root_dir=dest_root)
        zip_path = Path(zip_file)

    return dest_root, zip_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a clean, shareable backup workspace with sensitive data and caches removed."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.cwd(),
        help="Source workspace directory. Default: current directory.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.cwd().parent,
        help="Directory where the clean copy will be created. Default: parent of current directory.",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip zip archive creation.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source.resolve()
    output_root = args.output_root.resolve()

    if not source_root.exists() or not source_root.is_dir():
        raise SystemExit(f"source directory does not exist: {source_root}")

    output_root.mkdir(parents=True, exist_ok=True)

    dest_root, zip_path = build_clean_env(
        source_root=source_root,
        output_root=output_root,
        with_zip=not args.no_zip,
    )

    print(f"Clean env created: {dest_root}")
    if zip_path:
        print(f"Zip archive created: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
