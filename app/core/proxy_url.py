"""Proxy URL helpers shared by settings and the bot manager (no secrets in messages).

Supported schemes are those of ``aiohttp_socks`` / ``python_socks``
(``parse_proxy_url``): ``socks5``, ``socks4``, ``http``. Credentials go into the URL
(``socks5://user:pass@host:port``). :func:`mask_proxy_url` keeps only ``scheme://host:port``.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

__all__ = ["PROXY_URL_SCHEMES", "mask_proxy_url", "validate_proxy_url"]

PROXY_URL_SCHEMES: Final[frozenset[str]] = frozenset({"socks5", "socks4", "http"})


def mask_proxy_url(url: str) -> str:
    """``scheme://host:port`` without user/password/path; never raises."""
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<invalid proxy url>"
    scheme = parts.scheme.lower() or "?"
    if not host:
        return f"{scheme}://?"
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}:{port}" if port is not None else f"{scheme}://{host}"


def validate_proxy_url(url: str) -> str:
    """Return the stripped URL or raise ``ValueError`` (message without credentials)."""
    value = str(url).strip()
    parts = urlsplit(value)
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError(f"Malformed proxy URL: {mask_proxy_url(value)}") from None
    if parts.scheme.lower() not in PROXY_URL_SCHEMES:
        allowed = ", ".join(sorted(PROXY_URL_SCHEMES))
        raise ValueError(
            f"Unsupported proxy scheme {parts.scheme.lower()!r} ({allowed} expected): "
            f"{mask_proxy_url(value)}"
        )
    if not host or port is None:
        raise ValueError(f"Proxy URL must contain host and port: {mask_proxy_url(value)}")
    return value
