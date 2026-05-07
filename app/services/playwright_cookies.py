from urllib.parse import urlparse


def parse_cookie_string_for_url(cookie_str: str, url: str) -> list[dict]:
    """Convert a Cookie header string into Playwright cookie objects."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []

    cookies = []
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "url": url,
                "path": "/",
            }
        )
    return cookies


def cookies_to_header(cookies: list[dict]) -> str:
    seen = {}
    for cookie in cookies or []:
        name = (cookie.get("name") or "").strip()
        value = cookie.get("value")
        if name and value is not None:
            seen[name] = str(value)
    return "; ".join(f"{name}={value}" for name, value in seen.items())
