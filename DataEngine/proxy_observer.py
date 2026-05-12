from __future__ import annotations

from urllib.parse import urlparse


def mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return "Direct"
    text = str(proxy).strip()
    if not text:
        return "Direct"
    parsed = urlparse(text if "://" in text else f"proxy://{text}")
    host = parsed.hostname or text.split("@")[-1].split(":")[0]
    if not host:
        return "Proxy"
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        host = f"{parts[0]}.{parts[1]}.*.*"
    elif len(host) > 8:
        host = f"{host[:4]}...{host[-4:]}"
    port = f":{parsed.port}" if parsed.port else ""
    return f"Proxy: {host}{port}"


def proxy_tag(proxy: str | None) -> str:
    return f"({mask_proxy(proxy)})"
