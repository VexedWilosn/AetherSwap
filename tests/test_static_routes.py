import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_static_route_serves_files_inside_web_root(tmp_path, monkeypatch):
    from app.routes import static

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    asset = web_dir / "app.js"
    asset.write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(static, "WEB_DIR", web_dir)

    response = static.static_or_index("app.js")

    assert Path(response.path) == asset
    assert response.headers["cache-control"] == "no-cache"


def test_static_route_blocks_path_traversal(tmp_path, monkeypatch):
    from app.routes import static

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    index = web_dir / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(static, "WEB_DIR", web_dir)

    response = static.static_or_index("../secret.txt")

    assert Path(response.path) == index
    assert Path(response.path) != secret
    assert response.headers["cache-control"] == "no-cache"


def test_versioned_settings_assets_and_uncached_api_fetches():
    root = Path(__file__).resolve().parent.parent
    index_html = (root / "web" / "index.html").read_text(encoding="utf-8")
    utils_js = (root / "web" / "js" / "utils.js").read_text(
        encoding="utf-8"
    )
    settings_js = (root / "web" / "js" / "settings.js").read_text(
        encoding="utf-8"
    )

    assert '<script src="/js/settings.js?v=2"></script>' in index_html
    assert '<script src="/js/main.js?v=5"></script>' in index_html
    assert 'cache: opts.cache ?? "no-store"' in utils_js
    save_start = settings_js.index("async function saveConfigFromForm()")
    save_end = settings_js.index("async function startPipeline()", save_start)
    save_source = settings_js[save_start:save_end]
    assert save_source.index("const pending = formToConfig();") < save_source.index(
        "await fetchJson"
    )
    assert 'fetchJson(API + "/config");' not in save_source
