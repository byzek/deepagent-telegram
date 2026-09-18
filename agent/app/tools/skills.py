"""Tool for installing external skills into the current user's `/skills/`.

Wraps `app.skills_install` (pure fetch/parse logic) as a LangChain tool bound to
the per-user store, so the agent can pull a SKILL.md-style library on command.
"""
from __future__ import annotations

import logging

from langchain.tools import ToolRuntime, tool

from app.config import settings
from app.skills_install import install_skills

log = logging.getLogger(__name__)


def build_skill_tools() -> list:
    @tool
    async def install_skill(source: str, runtime: ToolRuntime) -> str:
        """Install reusable skills — including their runnable code — from an
        external source into YOUR library for the current user. Use this when
        the user directs you to add/learn skills from a repo or link; you fetch
        and install everything yourself, the user configures nothing.

        `source` accepts:
        - `owner/repo` or `owner/repo/sub/path` (GitHub, default branch)
        - a GitHub repo / `tree` / `blob` URL
        - a direct link to a `.md` skill file

        A `SKILL.md`-defined skill installs its playbook into `/skills/` (for
        semantic recall) AND materializes its whole folder — scripts included —
        into the sandbox at `/workspace/skills/<name>/`, so you can immediately
        run its code with the `execute` tool. Repos of loose `*.md` guides (no
        SKILL.md) install as playbooks only. Installing overwrites (updates) a
        skill of the same name. Returns the installed skills and where they live."""
        user_id = runtime.context.user_id
        try:
            installed = await install_skills(
                runtime.store, user_id, source,
                token=settings.github_token,
                sandbox_url=settings.sandbox_url,
                sandbox_token=settings.sandbox_token,
            )
        except ValueError as exc:
            return f"BAD_SOURCE: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface as tool output, never crash the turn
            log.warning("install_skill failed for %r: %s", source, exc)
            return f"INSTALL_ERROR: {exc}"

        if not installed:
            return f"NO_SKILLS_FOUND in {source!r} (looked for SKILL.md / *.md)."
        lines = [f"Installed {len(installed)} skill(s):"]
        for i in installed:
            if i["sandbox_path"]:
                lines.append(
                    f"- {i['key']}  (+{i['files']} file(s) in {i['sandbox_path']})"
                )
            else:
                lines.append(f"- {i['key']}  (playbook only, from {i['path']})")
        return "\n".join(lines)

    return [install_skill]
