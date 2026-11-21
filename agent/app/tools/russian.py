"""Flashcard tools for Russian practice (see app.russian)."""
from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app import russian
from app.config import settings


def build_russian_tools(pool) -> list:
    tz = settings.scheduler_tz()

    @tool
    async def russian_add_card(russian_text: str, english: str, runtime: ToolRuntime,
                               example: str = "", note: str = "") -> str:
        """Save a Russian flashcard for the current user (spaced repetition).
        Use during Russian practice when the user asks how to say something or
        makes a mistake worth drilling. `russian_text` is the natural Russian
        phrase (front), `english` its meaning (back), `example` a short Russian
        sentence using it, `note` an optional usage/register hint. Re-adding an
        existing phrase updates it and makes it due again."""
        cid, created = await russian.add_card(
            pool, runtime.context.user_id, russian_text, english, example, note
        )
        return f"{'Added' if created else 'Updated'} card #{cid}: {russian_text} = {english}"

    @tool
    async def russian_due_cards(runtime: ToolRuntime, limit: int = 10) -> str:
        """List the current user's Russian flashcards that are due for review."""
        rows = await russian.due_cards(pool, runtime.context.user_id, max(1, min(limit, 50)))
        if not rows:
            return "NO_DUE_CARDS"
        return "\n".join(f"#{cid}: {front} = {back}" + (f" (e.g. {ex})" if ex else "")
                         for cid, front, back, ex in rows)

    @tool
    async def russian_review_card(card_id: int, grade: int, runtime: ToolRuntime) -> str:
        """Record how well the user recalled a Russian flashcard: grade 0-5
        (0 = no idea, 3 = correct with effort, 5 = instant). Reschedules it."""
        due = await russian.review_card(pool, runtime.context.user_id, card_id, grade)
        if due is None:
            return f"NOT_FOUND: no card #{card_id} for this user."
        return f"Card #{card_id} next due {due.astimezone(tz):%Y-%m-%d}."

    return [russian_add_card, russian_due_cards, russian_review_card]
