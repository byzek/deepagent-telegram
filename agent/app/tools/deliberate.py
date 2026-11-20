"""`deliberate` tool: sample N answers in parallel, let a judge pick the best.
See app/deliberate.py for the trigger word and prompts."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from langchain.tools import tool
from langchain_openai import ChatOpenAI

from app.config import settings
from app.deliberate import candidate_prompt, judge_prompt, pick_index

log = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _model(temperature: float) -> ChatOpenAI:
    idle = settings.llm_stream_idle_timeout or None
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature,
        max_tokens=settings.llm_max_tokens,
        streaming=True,
        stream_usage=True,
        max_retries=0,
        timeout=httpx.Timeout(connect=30.0, read=idle, write=30.0, pool=30.0),
    )


def _text(msg) -> str:
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return _THINK.sub("", content or "").strip()


def build_deliberate_tools() -> list:
    sampler = _model(settings.deliberate_temperature)
    judge = _model(0.0)

    @tool
    async def deliberate(question: str, context: str = "") -> str:
        """Best-of-N answering for hard questions. ONLY use when the user's turn
        is marked [Deliberate mode]. Generates several independent answers to
        `question` (given the facts in `context`) and returns the one a judge
        rates best. Gather facts with your other tools BEFORE calling this; it
        has no tools of its own."""
        n = max(2, settings.deliberate_n)
        prompt = candidate_prompt(question, context)
        results = await asyncio.gather(
            *(sampler.ainvoke(prompt) for _ in range(n)), return_exceptions=True
        )
        candidates = [_text(r) for r in results if not isinstance(r, BaseException)]
        candidates = [c for c in candidates if c]
        if not candidates:
            return f"DELIBERATE_ERROR: all {n} samples failed: {results[0]}"
        if len(candidates) == 1:
            return candidates[0]
        verdict = await judge.ainvoke(judge_prompt(question, context, candidates))
        best, reason = pick_index(_text(verdict), len(candidates))
        log.info("deliberate: picked %d/%d (%s)", best, len(candidates), reason)
        return f"{candidates[best - 1]}\n\n[deliberate: picked answer {best} of {len(candidates)}. {reason}]"

    return [deliberate]
