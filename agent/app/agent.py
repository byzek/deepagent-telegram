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
from deepagents import MemoryMiddleware
from deepagents.backends import CompositeBackend, StoreBackend
from langchain.agents.middleware import LLMToolSelectorMiddleware, TodoListMiddleware
from langchain_openai import ChatOpenAI

from app.backend_sandbox import SandboxBackend
from app.config import settings
from app.prompt import system_prompt
from app.tools import (
    build_deliberate_tools,
    build_image_tools,
    build_memory_tools,
    build_network_tools,
    build_schedule_tools,
    build_search_tools,
    build_skill_tools,
)

log = logging.getLogger(__name__)

# Never filtered by the decider: the deepagents harness (files, shell, planning,
# subagents) and the recall tools the system prompt tells the agent to use
# before acting. Everything else — including tools added later — is selectable.
ALWAYS_AVAILABLE_TOOLS = [
    "write_todos", "ls", "read_file", "write_file", "edit_file", "delete",
    "glob", "grep", "execute", "task", "search_memory", "search_skills",
    "deliberate",
]

# Injected into the system prompt every turn (per-user, via the /memories/ route).
MEMORY_SOURCES = ["/memories/instructions.md"]


@dataclass
class Context:
    """Per-run context. `user_id` scopes this user's private memory/skills;
    `chat_id` is where scheduled tasks created during this run will reply."""

    user_id: str
    chat_id: str = ""


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
        # deepagents' UnsupportedContentMiddleware reads this to strip blocks the
        # model can't take; with no profile it assumes everything is supported.
        profile={
            "image_inputs": settings.llm_vision,
            "image_tool_message": settings.llm_vision,
        },
    )


class _ToolSelector(LLMToolSelectorMiddleware):
    """Fails open: if the decider errors or picks nonexistent tools, the main
    model runs with every tool. Main-model errors still propagate untouched."""

    async def awrap_model_call(self, request, handler):
        called = False

        async def tracked(req):
            nonlocal called
            called = True
            return await handler(req)

        try:
            return await super().awrap_model_call(request, tracked)
        except Exception as exc:  # noqa: BLE001 - a bad decision must never cost a reply
            if called:
                raise
            log.warning("tool selection failed, using all tools: %s", exc)
            return await handler(request)


def _build_tool_selector() -> _ToolSelector | None:
    if not settings.llm_decider_model:
        return None
    decider = ChatOpenAI(
        model=settings.llm_decider_model,
        base_url=settings.llm_decider_base_url,
        api_key=settings.llm_decider_api_key,
        temperature=0,
        max_tokens=256,
        max_retries=0,
        timeout=settings.llm_decider_timeout,
    )
    return _ToolSelector(
        model=decider,
        always_include=ALWAYS_AVAILABLE_TOOLS,
        on_parsing_failure="all",
    )


class _Memory(MemoryMiddleware):
    """Reloads every turn. Upstream loads once per thread and checkpoints it,
    but our threads live until /reset, so edits would never reach the prompt."""

    async def abefore_agent(self, state, runtime, config):
        state = {k: v for k, v in state.items() if k != "memory_contents"}
        return await super().abefore_agent(state, runtime, config)


def _build_memory(backend) -> _Memory:
    return _Memory(backend=backend, sources=MEMORY_SOURCES)


def _build_backend() -> CompositeBackend:
    """Default backend is the sandbox: the file tools AND the built-in `execute`
    tool share the isolated container's real `/workspace` (one filesystem, no
    in-graph scratch). `/memories/` and `/skills/` are routed to the Postgres
    store, namespaced per user so each person's data is private and durable."""
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
        # Middleware offloads (conversation history, large tool results) go
        # here; the sandbox root is read-only, only /workspace is writable.
        artifacts_root="/workspace",
    )


# Tools the deepagents harness provides itself (not in our `tools` list).
HARNESS_TOOLS = [
    "write_todos", "ls", "read_file", "write_file", "edit_file", "delete",
    "glob", "grep", "execute", "task",
]


def build_tools(pool) -> list:
    # NOTE: no custom `execute` tool here — deepagents provides a built-in
    # `execute` that now works because our default backend is sandbox-capable.
    # A second tool named `execute` would collide with it.
    return [
        *build_search_tools(),
        *build_memory_tools(),
        *build_skill_tools(),
        *build_schedule_tools(pool),
        *build_image_tools(),
        *build_network_tools(pool),
        *build_deliberate_tools(),
    ]


def baked_tool_catalog(tools) -> list[dict]:
    """Every tool shipped in the image: harness built-ins + our Python tools."""
    return [{"name": n, "description": "deepagents harness built-in"} for n in HARNESS_TOOLS] + [
        {"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
        for t in tools
    ]


def build_agent(checkpointer, store, pool):
    """Return a compiled Deep Agent graph wired to durable persistence."""
    model = _build_model()
    tools = build_tools(pool)

    backend = _build_backend()
    middleware = [TodoListMiddleware(), _build_memory(backend)]
    selector = _build_tool_selector()
    if selector is not None:
        middleware.append(selector)

    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt(settings.agent_name),
        backend=backend,
        middleware=middleware,
        context_schema=Context,
        checkpointer=checkpointer,
        store=store,
    )
    agent.baked_tools = baked_tool_catalog(tools)
    log.info(
        "Deep Agent built: model=%s decider=%s tools=%s",
        settings.llm_model,
        settings.llm_decider_model or None,
        [getattr(t, "name", str(t)) for t in tools],
    )
    return agent
