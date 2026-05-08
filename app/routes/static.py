"""Static file serving routes – must be registered LAST (catch-all)."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse
router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _static_response(path: Path) -> FileResponse:
    if path.suffix.lower() in {".html", ".css", ".js"}:
        return FileResponse(path, headers=NO_CACHE_HEADERS)
    return FileResponse(path)


@router.get("/")
def index():
    f = WEB_DIR / "index.html"
    if f.exists():
        return _static_response(f)
    return {"app": "aetherswap", "ui": "web/index.html not found"}


@router.get("/{path:path}")
def static_or_index(path: str):
    f = WEB_DIR / path
    if f.is_file():
        return _static_response(f)
    if (WEB_DIR / "index.html").exists():
        return _static_response(WEB_DIR / "index.html")
    return {"error": "not found"}
