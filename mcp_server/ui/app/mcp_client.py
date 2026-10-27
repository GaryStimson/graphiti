"""Calls the memory server's ``remember`` tool, so UI-added memories are extracted exactly
like agent-added ones (same LLM, embeddings, supersession)."""

from __future__ import annotations

import json
from typing import Any

try:  # mcp >= 2 uses httpx2; older releases use httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import Settings


class RememberError(RuntimeError):
    pass


async def remember(
    settings: Settings,
    content: str,
    categories: list[str] | None = None,
    title: str | None = None,
    agent: str = 'memory-ui',
) -> dict[str, Any]:
    if not settings.mcp_secret_path:
        raise RememberError('MCP_SECRET_PATH is not configured for the UI container')
    arguments: dict[str, Any] = {'content': content, 'agent': agent}
    if categories:
        arguments['categories'] = categories
    if title:
        arguments['title'] = title
    try:
        async with httpx.AsyncClient(
            headers={'Host': settings.mcp_host_header}, timeout=httpx.Timeout(30.0)
        ) as http:
            async with streamable_http_client(settings.mcp_endpoint, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool('remember', arguments)
    except Exception as exc:  # network, protocol or server errors
        raise RememberError(f'Could not reach the memory server: {exc}') from exc

    text = '\n'.join(getattr(block, 'text', '') for block in result.content)
    is_error = getattr(result, 'is_error', getattr(result, 'isError', False))
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {'message': text}
    if is_error or 'error' in payload:
        raise RememberError(str(payload.get('error') or payload.get('message') or text))
    return payload
