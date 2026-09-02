"""Deep Agent construction.

Wires the private OpenAI-compatible model, persistent per-user memory/skills
backend, search + sandbox tools, task planning, and Postgres durability into a
single compiled graph.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_openai import ChatOpenAI

from app.config import settings
from app.prompt import system_prompt
from app.tools import build_memory_tools, build_sandbox_tools, build_search_tools

log = logging.getLogger(__name__)


@dataclass
class Context:
    """Per-run context. `user_id` scopes this user's private memory/skills."""

    user_id: str


def _build_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout=settings.llm_request_timeout,
    )


def _build_backend() -> CompositeBackend:
    """Thread-scoped scratch by default; `/memories/` and `/skills/` persist in
    the Postgres store, namespaced per user so each person's data is private."""
    return CompositeBackend(
        default=StateBackend(),
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
    tools = [
        *build_search_tools(),
        *build_sandbox_tools(),
        *build_memory_tools(),
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
