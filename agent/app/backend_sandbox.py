"""Sandbox-capable backend: virtual FS in-graph + shell execution in the
isolated `sandbox` container.

deepagents 0.7.x couples the filesystem and the `execute` tool into a single
"backend": the built-in `execute` tool only activates when the (default)
backend implements `SandboxBackendProtocol`. A plain `StateBackend` does NOT,
which is why the agent reports it "has no sandbox".

This backend inherits `StateBackend` for the per-thread virtual filesystem
(`ls`/`read_file`/`write_file`/`grep`/… operate on in-graph state, so the model
can never read the agent container's real files/secrets) and adds `execute` by
proxying to the hardened `sandbox` container over its authenticated HTTP API.
Shell commands therefore run in the isolated container (its own `/workspace`),
while file tools stay virtual — the same split the system prompt describes.
"""
from __future__ import annotations

import logging

import httpx
from deepagents.backends import StateBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from app.config import settings

log = logging.getLogger(__name__)


def _to_response(data: dict) -> ExecuteResponse:
    """Map the sandbox service's JSON into deepagents' ExecuteResponse."""
    stdout = data.get("stdout", "") or ""
    stderr = data.get("stderr", "") or ""
    code = data.get("exit_code")
    timed_out = data.get("timed_out", False)

    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.extend(f"[stderr] {line}" for line in stderr.rstrip("\n").split("\n"))
    output = "\n".join(parts) if parts else "<no output>"
    if timed_out:
        output += "\n\nError: command timed out and was killed."
    return ExecuteResponse(output=output, exit_code=code, truncated=False)


def _url() -> str:
    return f"{settings.sandbox_url}/execute"


def _headers() -> dict:
    return {"X-Sandbox-Token": settings.sandbox_token}


def _client_timeout(timeout: int | None) -> float:
    # Give the HTTP call more slack than the in-sandbox command timeout so the
    # sandbox's own timeout (and a clean JSON error) wins the race.
    return (timeout or settings.sandbox_timeout) + 15


class SandboxBackend(StateBackend, SandboxBackendProtocol):
    """StateBackend virtual FS + `execute` proxied to the sandbox container."""

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            async with httpx.AsyncClient(timeout=_client_timeout(timeout)) as client:
                resp = await client.post(_url(), json={"command": command}, headers=_headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 - surface as tool output, never crash the turn
            log.warning("Sandbox execute failed: %s", exc)
            return ExecuteResponse(output=f"SANDBOX_ERROR: {exc}", exit_code=1, truncated=False)
        return _to_response(data)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        try:
            with httpx.Client(timeout=_client_timeout(timeout)) as client:
                resp = client.post(_url(), json={"command": command}, headers=_headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Sandbox execute failed: %s", exc)
            return ExecuteResponse(output=f"SANDBOX_ERROR: {exc}", exit_code=1, truncated=False)
        return _to_response(data)
