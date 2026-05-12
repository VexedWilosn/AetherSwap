"""Static file serving routes - must be registered LAST (catch-all)."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


@router.get("/")
def index():
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(status_code=500, content={"error": f"{index_file} not found"})


@router.get("/{path:path}")
def static_or_index(path: str):
    if path.startswith(("api/", "docs", "openapi.json")):
        return JSONResponse(status_code=404, content={"error": "not found"})
    target = (WEB_DIR / path).resolve()
    try:
        target.relative_to(WEB_DIR.resolve())
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "not found"})
    if target.is_file():
        return FileResponse(target)
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(status_code=500, content={"error": f"{index_file} not found"})
