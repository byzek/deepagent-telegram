"""Per-user semantic memory search tools.

These let the agent retrieve relevant long-term memories/skills by MEANING, not
just literal filename/grep matching. They query the configured memory backend's
vector index, scoped to the current user's namespace.
"""
from __future__ import annotations

import logging

from langchain.tools import ToolRuntime, tool

from app.config import settings

log = logging.getLogger(__name__)


def _format(items) -> str:
    if not items:
        return "NO_MATCHES"
    lines = []
    for it in items:
        score = getattr(it, "score", None)
        content = ""
        if isinstance(it.value, dict):
            content = it.value.get("content", "") or ""
        content = content.strip().replace("\n", " ")
        if len(content) > 500:
            content = content[:500] + "…"
        tag = f"[{score:.3f}] " if isinstance(score, (int, float)) else ""
        lines.append(f"- {tag}{it.key}: {content}")
    return "\n".join(lines)


def build_memory_tools() -> list:
    limit = settings.memory_search_limit

    @tool
    async def search_memory(query: str, runtime: ToolRuntime) -> str:
        """Semantically search YOUR long-term memories for the current user.
        Use this to recall relevant past facts/preferences before answering,
        especially when the user references something from an earlier session.
        Returns the most relevant memory snippets with similarity scores."""
        user_id = runtime.context.user_id
        items = await runtime.store.asearch(
            ("memories", user_id), query=query, limit=limit
        )
        return _format(items)

    @tool
    async def search_skills(query: str, runtime: ToolRuntime) -> str:
        """Semantically search YOUR skills (reusable playbooks) for the current
        user. Use this to find the right procedure before doing a recurring
        task. Returns the most relevant skill snippets with similarity scores."""
        user_id = runtime.context.user_id
        items = await runtime.store.asearch(
            ("skills", user_id), query=query, limit=limit
        )
        return _format(items)

    return [search_memory, search_skills]
