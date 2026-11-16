"""Threads live until /reset, so memory must reload each turn: an edit to
/memories/instructions.md has to reach the next turn's prompt, not the next thread's.

Run with pytest:      pytest tests/test_memory.py   (from agent/)
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deepagents.backends.protocol import FileDownloadResponse  # noqa: E402

from app import agent  # noqa: E402


class _Backend:
    def __init__(self, content: str):
        self.content = content

    async def adownload_files(self, paths):
        return [FileDownloadResponse(path=p, content=self.content.encode()) for p in paths]


def test_memory_reloads_every_turn():
    backend = _Backend("prefers metric units")
    mw = agent._build_memory(backend)
    stale = {"messages": [], "memory_contents": {agent.MEMORY_SOURCES[0]: "old"}}
    update = asyncio.run(mw.abefore_agent(stale, None, None))
    assert update == {"memory_contents": {agent.MEMORY_SOURCES[0]: "prefers metric units"}}


def test_memory_reads_seeded_instructions():
    assert agent.MEMORY_SOURCES == ["/memories/instructions.md"]
