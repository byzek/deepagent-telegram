"""Tools that let the agent schedule future work for the current user/chat.

Jobs are executed by `app.scheduler.scheduler_loop`, which runs each one as a
normal agent turn in the chat that created it and sends the reply there.
"""
from __future__ import annotations

from datetime import datetime, timezone

from langchain.tools import ToolRuntime, tool

from app import scheduler
from app.config import settings


def _who(runtime: ToolRuntime) -> tuple[str, str, str]:
    platform, user_id = runtime.context.user_id.split(":", 1)
    return platform, user_id, runtime.context.chat_id


def _now_line(tz) -> str:
    return f"Current time: {datetime.now(tz).isoformat(timespec='minutes')} ({tz.key})"


def build_schedule_tools(pool) -> list:
    tz = settings.scheduler_tz()

    @tool
    async def schedule_task(
        prompt: str,
        runtime: ToolRuntime,
        run_at: str | None = None,
        in_minutes: int | None = None,
        every_minutes: int | None = None,
    ) -> str:
        """Schedule YOURSELF to run later in this chat — reminders, recurring
        check-ins, periodic monitoring, follow-ups. When it fires, `prompt` is
        delivered to you as a new turn (with all your tools) and your reply is
        sent to the user, even if they haven't messaged you.

        Give exactly one of:
        - `run_at`: ISO 8601 datetime, e.g. "2026-10-04T08:00". Without an
          offset it is interpreted in the configured local timezone.
        - `in_minutes`: fire this many minutes from now.
        Optionally `every_minutes` to repeat (1440 = daily, 10080 = weekly);
        repeats keep the same local wall-clock time across DST.

        Write `prompt` as an instruction to your future self, e.g.
        "Remind the user to call the dentist." or "Check BTC price and report
        if it moved more than 5% since yesterday." Call `list_scheduled_tasks`
        first if you need the current time."""
        platform, user_id, chat_id = _who(runtime)
        if every_minutes is not None and every_minutes < 1:
            return "BAD_SCHEDULE: every_minutes must be >= 1"
        try:
            when = scheduler.first_run(
                datetime.now(timezone.utc), tz, run_at=run_at, in_minutes=in_minutes
            )
        except ValueError as exc:
            return f"BAD_SCHEDULE: {exc}\n{_now_line(tz)}"
        job_id = await scheduler.add_job(
            pool, platform, user_id, chat_id, prompt, when, every_minutes
        )
        repeat = f", then every {every_minutes} min" if every_minutes else ""
        return (
            f"Scheduled task #{job_id}: first run "
            f"{when.astimezone(tz).isoformat(timespec='minutes')}{repeat}."
        )

    @tool
    async def list_scheduled_tasks(runtime: ToolRuntime) -> str:
        """List the current user's scheduled tasks and show the current time."""
        platform, user_id, _ = _who(runtime)
        rows = await scheduler.list_jobs(pool, platform, user_id)
        lines = [_now_line(tz)]
        if not rows:
            lines.append("NO_SCHEDULED_TASKS")
        for job_id, nxt, every, prompt in rows:
            repeat = f" (every {every} min)" if every else " (once)"
            lines.append(
                f"- #{job_id} next {nxt.astimezone(tz).isoformat(timespec='minutes')}"
                f"{repeat}: {prompt}"
            )
        return "\n".join(lines)

    @tool
    async def cancel_scheduled_task(job_id: int, runtime: ToolRuntime) -> str:
        """Cancel one of the current user's scheduled tasks by its id."""
        platform, user_id, _ = _who(runtime)
        if await scheduler.cancel_job(pool, platform, user_id, job_id):
            return f"Cancelled task #{job_id}."
        return f"NOT_FOUND: no task #{job_id} for this user."

    return [schedule_task, list_scheduled_tasks, cancel_scheduled_task]
