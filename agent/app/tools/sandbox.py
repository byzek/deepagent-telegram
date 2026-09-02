"""Sandbox execution tool.

Exposes a single `execute` tool that runs a shell command inside the isolated
`sandbox` container (non-root, no host access, resource-limited). The agent
uses this to run code, install packages, and work with a real filesystem in
`/workspace`, which persists across conversations via a Docker volume.

The agent server itself never runs arbitrary code — it only makes an
authenticated HTTP call to the sandbox service.
"""
from __future__ import annotations

import logging

import httpx
from langchain_core.tools import tool

from app.config import settings

log = logging.getLogger(__name__)


def build_sandbox_tools() -> list:
    @tool
    async def execute(command: str) -> str:
        """Run a shell command in the isolated sandbox (working dir: /workspace,
        which persists between runs). Use this to run code, install Python
        packages with `pip install --user ...`, inspect files, and produce
        artifacts. git/curl/jq are preinstalled. Returns combined stdout/stderr
        and the exit code. Long-running commands are killed after the timeout."""
        payload = {"command": command}
        headers = {"X-Sandbox-Token": settings.sandbox_token}
        try:
            async with httpx.AsyncClient(timeout=settings.sandbox_timeout + 15) as client:
                resp = await client.post(
                    f"{settings.sandbox_url}/execute",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Sandbox execute failed: %s", exc)
            return f"SANDBOX_ERROR: {exc}"

        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        code = data.get("exit_code")
        timed_out = data.get("timed_out", False)

        parts = [f"exit_code={code}" + (" (TIMED OUT)" if timed_out else "")]
        if stdout:
            parts.append("stdout:\n" + stdout)
        if stderr:
            parts.append("stderr:\n" + stderr)
        return "\n".join(parts)

    return [execute]
