"""Web search tools.

Primary:  `web_search`   -> self-hosted SearXNG (private, no API key).
Fallback: `brave_search` -> Brave Search API (CAPTCHA-proof, needs BRAVE_API_KEY).

The agent is instructed (in the system prompt) to try `web_search` first and
fall back to `brave_search` when SearXNG returns nothing / is rate-limited.
"""
from __future__ import annotations

import logging

import httpx
from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger(__name__)

_MAX_RESULTS = 8


def _format_results(items: list[dict]) -> str:
    if not items:
        return "NO_RESULTS"
    lines: list[str] = []
    for i, r in enumerate(items[:_MAX_RESULTS], start=1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or r.get("href") or "").strip()
        snippet = (r.get("content") or r.get("description") or r.get("snippet") or "").strip()
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n".join(lines)


def build_search_tools() -> list:
    tools: list = []

    @tool
    async def web_search(query: str) -> str:
        """Search the web via the private SearXNG instance. Use this FIRST for
        any web lookup. Returns a numbered list of titles, URLs, and snippets.
        If it returns 'NO_RESULTS' or an error, retry with `brave_search`."""
        params = {
            "q": query,
            "format": "json",
            "safesearch": "0",
            "language": "en",
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{settings.searxng_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 - surface a usable message to the model
            log.warning("SearXNG search failed: %s", exc)
            return f"SEARXNG_ERROR: {exc}. Try brave_search instead."
        return _format_results(data.get("results", []))

    tools.append(web_search)

    if settings.brave_api_key:

        @tool
        async def brave_search(query: str) -> str:
            """Search the web via the Brave Search API. Use this as a FALLBACK
            when `web_search` (SearXNG) fails, is rate-limited, or returns
            nothing. Returns a numbered list of titles, URLs, and snippets."""
            headers = {
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            }
            params = {"q": query, "count": _MAX_RESULTS}
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        headers=headers,
                        params=params,
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("Brave search failed: %s", exc)
                return f"BRAVE_ERROR: {exc}"
            results = (data.get("web") or {}).get("results", [])
            return _format_results(results)

        tools.append(brave_search)
    else:
        log.info("BRAVE_API_KEY not set — brave_search fallback disabled.")

    return tools
