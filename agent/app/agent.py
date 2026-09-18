"""Deep Agent construction.

Wires the private OpenAI-compatible model, persistent per-user memory/skills
backend, search + sandbox tools, task planning, and Postgres durability into a
single compiled graph.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_openai import ChatOpenAI

from app.backend_sandbox import SandboxBackend
from app.config import settings
from app.prompt import system_prompt
from app.tools import build_memory_tools, build_search_tools, build_skill_tools

log = logging.getLogger(__name__)


@dataclass
class Context:
    """Per-run context. `user_id` scopes this user's private memory/skills."""

    user_id: str


def _build_model() -> ChatOpenAI:
    # Stream so the HTTP read timeout acts as an INACTIVITY timeout: httpx's
    # `read` bound is the max gap between received chunks, so as long as the
    # server keeps emitting tokens the request never times out — no matter how
    # long the full generation takes. A stalled/dead stream trips it after
    # `llm_stream_idle_timeout` seconds. (0 => wait indefinitely between chunks;
    # the absolute ceiling is enforced separately in dispatch.handle_turn.)
    idle = settings.llm_stream_idle_timeout or None
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        streaming=True,
        # Keep token usage flowing under streaming so the dashboard stays accurate.
        stream_usage=True,
        # Don't auto-retry: a genuine stall shouldn't silently restart (and
        # duplicate) a long generation; a working stream never trips the timeout.
        max_retries=0,
        timeout=httpx.Timeout(connect=30.0, read=idle, write=30.0, pool=30.0),
    )


def _build_backend() -> CompositeBackend:
    """Default backend is sandbox-capable: virtual scratch FS in-graph PLUS the
    built-in `execute` tool wired to the isolated sandbox container. `/memories/`
    and `/skills/` persist in the Postgres store, namespaced per user so each
    person's data is private."""
    return CompositeBackend(
        default=SandboxBackend(),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda rt: ("memories", rt.context.user_id),
            ),
            "/skills/": StoreBackend(
                namespace=lambda rt: ("skills", rt.context.user_id),
            ),
        },
    )


def build_agent(checkpointer, store):
    """Return a compiled Deep Agent graph wired to durable persistence."""
    model = _build_model()
    # NOTE: no custom `execute` tool here — deepagents provides a built-in
    # `execute` that now works because our default backend is sandbox-capable.
    # A second tool named `execute` would collide with it.
    tools = [
        *build_search_tools(),
        *build_memory_tools(),
        *build_skill_tools(),
    ]

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt(settings.agent_name),
        backend=_build_backend(),
        middleware=[TodoListMiddleware()],
        context_schema=Context,
        checkpointer=checkpointer,
        store=store,
    )
    log.info(
        "Deep Agent built: model=%s tools=%s",
        settings.llm_model,
        [getattr(t, "name", str(t)) for t in tools],
    )
    return agent
