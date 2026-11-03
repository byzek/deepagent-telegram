"""The decider model trims the tool list per turn, never strips the harness and
recall tools, and fails open: a broken decider must never cost a reply.

Run with pytest:      pytest tests/test_tool_selector.py   (from agent/)
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from langchain.agents.middleware import ModelRequest  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

from app import agent  # noqa: E402
from app.metrics import TurnMetrics  # noqa: E402

SELECTABLE = ["web_search", "schedule_task", "generate_image"]


class _Decider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _tool(name):
    return StructuredTool.from_function(lambda: "", name=name, description=f"{name} tool")


def _request():
    tools = [_tool(n) for n in [*agent.ALWAYS_AVAILABLE_TOOLS, *SELECTABLE]]
    return ModelRequest(model=None, messages=[HumanMessage("remind me at 8")], tools=tools)


def _selector(monkeypatch, reply):
    monkeypatch.setattr(
        agent, "settings", dataclasses.replace(agent.settings, llm_decider_model="small")
    )
    mw = agent._build_tool_selector()
    mw.model = _Decider(reply)
    return mw


def _run(mw, handler):
    seen = []

    async def record(req):
        seen.append(sorted(t.name for t in req.tools))
        return await handler(req)

    async def go():
        return await mw.awrap_model_call(_request(), record)

    return asyncio.run(go()), seen


async def _ok(req):
    return AIMessage("done")


def test_disabled_without_decider_model(monkeypatch):
    monkeypatch.setattr(
        agent, "settings", dataclasses.replace(agent.settings, llm_decider_model="")
    )
    assert agent._build_tool_selector() is None


def test_decider_uses_its_own_model(monkeypatch):
    monkeypatch.setattr(
        agent, "settings", dataclasses.replace(agent.settings, llm_decider_model="small")
    )
    assert agent._build_tool_selector().model.model_name == "small"


def test_selection_trims_optional_tools_but_keeps_core(monkeypatch):
    mw = _selector(monkeypatch, {"tools": ["schedule_task"]})
    _, seen = _run(mw, _ok)
    assert seen == [sorted([*agent.ALWAYS_AVAILABLE_TOOLS, "schedule_task"])]


def test_decider_error_falls_back_to_all_tools(monkeypatch):
    mw = _selector(monkeypatch, RuntimeError("decider down"))
    _, seen = _run(mw, _ok)
    assert seen == [sorted([*agent.ALWAYS_AVAILABLE_TOOLS, *SELECTABLE])]


def test_hallucinated_tool_falls_back_to_all_tools(monkeypatch):
    mw = _selector(monkeypatch, {"tools": ["no_such_tool"]})
    _, seen = _run(mw, _ok)
    assert seen == [sorted([*agent.ALWAYS_AVAILABLE_TOOLS, *SELECTABLE])]


def test_malformed_output_falls_back_to_all_tools(monkeypatch):
    mw = _selector(monkeypatch, "not json")
    _, seen = _run(mw, _ok)
    assert seen == [sorted([*agent.ALWAYS_AVAILABLE_TOOLS, *SELECTABLE])]


def test_main_model_error_is_not_retried(monkeypatch):
    mw = _selector(monkeypatch, {"tools": ["web_search"]})

    async def boom(req):
        raise TimeoutError("main model stalled")

    with pytest.raises(TimeoutError):
        _run(mw, boom)


def test_always_available_tools_exist_in_the_real_agent(monkeypatch):
    monkeypatch.setattr(
        agent, "settings", dataclasses.replace(agent.settings, llm_decider_model="small")
    )
    graph = agent.build_agent(None, InMemoryStore(), None)
    assert set(agent.ALWAYS_AVAILABLE_TOOLS) <= set(graph.nodes["tools"].bound.tools_by_name)


def test_decider_calls_stay_out_of_turn_metrics():
    m = TurnMetrics()
    usage = {"input_tokens": 500, "output_tokens": 5, "total_tokens": 505}
    response = SimpleNamespace(
        generations=[[SimpleNamespace(message=AIMessage("x", usage_metadata=usage))]]
    )

    async def go():
        rid = uuid4()
        await m.on_chat_model_start({}, [], run_id=rid, metadata={"lc_source": "tool_selection"})
        await m.on_llm_end(response, run_id=rid)
        rid = uuid4()
        await m.on_chat_model_start({}, [], run_id=rid, metadata={})
        await m.on_llm_end(response, run_id=rid)

    asyncio.run(go())
    assert (m.calls, m.prompt_tokens) == (1, 500)
