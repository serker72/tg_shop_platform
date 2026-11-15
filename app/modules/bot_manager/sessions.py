"""aiogram HTTP sessions of the bots: one ``AiohttpSession`` per bot (research §5).

The proxy is a property of the aiohttp connector, so a session can not be shared by bots
of different proxies. Logs, exceptions and ``repr`` show only ``scheme://host:port``.
"""

from __future__ import annotations

from typing import Final

from aiogram.client.session.aiohttp import AiohttpSession

from app.core.proxy_url import mask_proxy_url, validate_proxy_url

__all__ = ["BOT_SESSION_CONNECTION_LIMIT", "build_bot_session", "mask_proxy_url"]

#: Connector limit of one bot (long polling + replies).
BOT_SESSION_CONNECTION_LIMIT: Final = 20


def build_bot_session(
    proxy_url: str | None, *, limit: int = BOT_SESSION_CONNECTION_LIMIT
) -> AiohttpSession:
    """Session of one bot; ``proxy_url=None`` - direct connection.

    Raises ``ValueError`` (credentials masked) for an unsupported/malformed proxy URL.
    """
    if proxy_url is None:
        return AiohttpSession(limit=limit)
    url = validate_proxy_url(proxy_url)
    try:
        return AiohttpSession(proxy=url, limit=limit)
    except (ValueError, RuntimeError) as error:
        raise ValueError(
            f"Can not build a bot session for proxy {mask_proxy_url(url)}: {type(error).__name__}"
        ) from None
