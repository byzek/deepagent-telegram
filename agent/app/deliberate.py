"""Deliberate mode: best-of-N answers for hard questions.

Start a message with the word `deliberate` (or `/deliberate`, `!deliberate`).
The agent first gathers facts with its normal tools, then calls the
`deliberate` tool, which samples N independent answers from the private model
in parallel and has a judge call pick the best one. Tokens are free locally, so
spending N+1 generations on one question is cheap insurance.

Helpers here are pure (stdlib only) so they're unit-testable.
"""
from __future__ import annotations

import json
import re

TRIGGER = "deliberate"
_TRIGGER_RE = re.compile(rf"^\s*[/!]?{TRIGGER}(?:@\w+)?(?![\w])[\s:,]*", re.IGNORECASE)
_JSON_RE = re.compile(r"\{[^{}]*\"best\"[^{}]*\}", re.DOTALL)
_NUM_RE = re.compile(r"\b(\d+)\b")


def split_trigger(text: str) -> tuple[bool, str]:
    m = _TRIGGER_RE.match(text or "")
    return (True, text[m.end():].strip()) if m else (False, text)


def wrap_turn(question: str) -> str:
    return (
        "[Deliberate mode: the user wants your most careful answer.] First gather "
        "any facts you need with your tools (search, memory, sandbox). Then call "
        "`deliberate(question=..., context=...)` exactly once, passing the question "
        "and everything relevant you found. Reply with the answer it returns; you "
        "may fix formatting but don't change its substance.\n\n" + question
    )


def candidate_prompt(question: str, context: str) -> str:
    ctx = f"Context gathered so far:\n{context}\n\n" if context.strip() else ""
    return (
        f"{ctx}Question:\n{question}\n\n"
        "Think it through step by step, then give a complete, self-contained final "
        "answer. Be concrete and state any assumptions."
    )


def judge_prompt(question: str, context: str, candidates: list[str]) -> str:
    body = "\n\n".join(f"### Answer {i}\n{c}" for i, c in enumerate(candidates, 1))
    ctx = f"Context:\n{context}\n\n" if context.strip() else ""
    return (
        f"{ctx}Question:\n{question}\n\n{body}\n\n"
        "You are a strict judge. Check each answer for correctness, use of the "
        "context, completeness and clarity. Prefer the answer whose conclusion most "
        "answers agree on unless it is clearly wrong. Reply with ONLY JSON: "
        '{"best": <answer number>, "reason": "<one sentence>"}'
    )


def pick_index(judge_output: str, n: int) -> tuple[int, str]:
    """Parse the judge's choice (1-based). Falls back to answer 1."""
    text = judge_output or ""
    m = _JSON_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(0))
            best = int(data.get("best"))
            if 1 <= best <= n:
                return best, str(data.get("reason", ""))
        except (ValueError, TypeError):
            pass
    else:
        for num in _NUM_RE.findall(text):
            if 1 <= int(num) <= n:
                return int(num), text.strip()[:200]
    return 1, "judge gave no valid choice"
