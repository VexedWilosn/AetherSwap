"""Config, data init, export/import, holdings report routes."""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from app.state import (
    clear_transactions,
    get_log,
    get_purchases,
    get_sales,
    replace_log,
    replace_transactions,
)
from app.config_loader import load_app_config_validated, save_app_config_validated
from config import load_app_config, save_app_config, save_credentials, get_all_credentials
from app.accounts import list_accounts, replace_all as accounts_replace_all
router = APIRouter()
class ConfigBody(BaseModel):
    config: dict
class ImportFullBody(BaseModel):
    app_config: dict = {}
    credentials: dict = {}
    transactions: dict = {}
    accounts: dict = {}
    log: list = []
@router.get("/api/config")
def api_get_config():
    return {"config": load_app_config_validated()}
@router.post("/api/config")
def api_save_config(body: ConfigBody):
    current = load_app_config_validated()
    merged = {**current, **body.config}
    for k, v in body.config.items():
        if isinstance(v, dict) and k in current and isinstance(current[k], dict):
            merged[k] = {**current[k], **v}
    save_app_config_validated(merged)
    return {"ok": True}
@router.post("/api/data/init")
def api_data_init():
    from app.state import clear_log
    from pathlib import Path
    import glob

    # 清理内存状态
    clear_transactions()
    clear_log()
    # 先使账号内存缓存失效，防止旧账号（含非人民币币种）残留在内存中
    import app.accounts as _accounts_mod
    _accounts_mod._cache = None
    accounts_replace_all({"accounts": [], "current_id": None})
    save_credentials({})

    # 构建一份干净的默认配置（保留功能性默认值，清除所有个人凭据）
    clean_config = {
        "buff": {
            "pay_method": "wechat",
            "game": "csgo",
        },
        "pipeline": {
            "max_discount": 0.8,
            "exclude_keywords": ["印花"],
        },
        "proxy_pool": {
            "enabled": False,
            "strategy": 1,
            "global_proxies": [],
            "steam_proxies": [],
            "buff_proxies": [],
            "uuyp_proxies": [],
            "proxies": [],
            "webshare_api_key": "",
        },
        # 以下敏感字段重置为空
        "steam_guard": {
            "shared_secret": "",
        },
        "steam_confirm": {
            "enabled": False,
            "identity_secret": "",
            "device_id": "",
        },
        "notify": {
            "pushplus_token": "",
            "email_user": "",
            "email_pass": "",
            "imap_server": "",
            "target_sender": "",
            "allowed_sender": "",
            "subject_success": "",
            "subject_fail": "",
        },
    }
    save_app_config_validated(clean_config)

    # 删除历史文件及缓存
    config_dir = Path("config")
    files_to_remove = [
        "exchange_rate.json",
        "holdings_report_last.json",
        "steam_userdata.json",
        "transactions.json.bak",
    ]
    for file_name in files_to_remove:
        file_path = config_dir / file_name
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass
                
    # 删除 Playwright 浏览器用户数据目录（包含 Cookie 等敏感信息）
    import shutil
    for dir_name in ["playwright_steam", "playwright_buff"]:
        dir_path = config_dir / dir_name
        if dir_path.exists() and dir_path.is_dir():
            try:
                shutil.rmtree(dir_path)
            except Exception:
                pass

    # 删除磁盘日志文件
    log_dir = Path("log")
    if log_dir.exists():
        for log_file in log_dir.glob("*.log"):
            try:
                log_file.unlink()
            except Exception:
                pass

    # 清空数据库 (丢弃所有表并重建)
    try:
        from app.database import get_engine, init_db
        from sqlmodel import SQLModel
        engine = get_engine()
        SQLModel.metadata.drop_all(engine)
        init_db()
    except Exception:
        pass

    disclaimer_file = Path(".agreed_disclaimer")
    if disclaimer_file.exists():
        try:
            disclaimer_file.unlink()
        except Exception:
            pass

    import os
    import threading
    def _shutdown():
        import time
        time.sleep(1.5)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()

    return {"ok": True}

@router.get("/api/export_full")
def api_export_full():
    from datetime import datetime, timezone
    from app.accounts import get_current_id
    data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_config": load_app_config(),
        "credentials": get_all_credentials(),
        "transactions": {"purchases": get_purchases(), "sales": get_sales()},
        "accounts": {"accounts": list_accounts(), "current_id": get_current_id()},
        "log": get_log(0),
    }
    return data

@router.get("/api/export_full/download")
def api_export_full_download():
    import json
    from datetime import datetime, timezone
    from fastapi.responses import Response
    from app.accounts import get_current_id
    data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_config": load_app_config(),
        "credentials": get_all_credentials(),
        "transactions": {"purchases": get_purchases(), "sales": get_sales()},
        "accounts": {"accounts": list_accounts(), "current_id": get_current_id()},
        "log": get_log(0),
    }
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    filename = f"full_backup_{ts}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
@router.post("/api/import_full")
def api_import_full(body: ImportFullBody):
    try:
        if body.app_config:
            save_app_config(body.app_config)
            from app.config_loader import _invalidate_config_cache
            _invalidate_config_cache()
        if body.credentials:
            save_credentials(body.credentials)
        tx = body.transactions or {}
        replace_transactions(tx.get("purchases", []), tx.get("sales", []))
        if body.accounts:
            accounts_replace_all(body.accounts)
        if body.log is not None:
            replace_log(body.log)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
@router.post("/api/holdings_report/send")
def api_holdings_report_send(force: bool = Query(False)):
    from app.services.workers import run_holdings_report_once
    ok = run_holdings_report_once(force=force)
    return {"ok": ok, "message": "已发送" if ok else "未发送(无持有/无Token/无Steam凭证)"}
