"""Sandbox-backed backend: one real filesystem + shell execution, both in the
isolated `sandbox` container.

deepagents 0.7.x couples the filesystem and the `execute` tool into a single
"backend". `BaseSandbox` derives every file tool (`ls`/`read_file`/`write_file`/
`edit`/`grep`/`glob`) from two primitives — `execute()` and `upload_files()` —
so a backend that implements those over the sandbox gets a filesystem and the
`execute` tool that both point at the SAME place: the sandbox's `/workspace`.

That is the whole point of this backend. There is no separate in-graph scratch
space anymore: what `execute` writes, `read_file` reads, and vice-versa. Only
`/memories/` and `/skills/` are special — `agent.py` routes those to a
persistent, per-user `StoreBackend`, so genuinely durable data still lives in
Postgres while ephemeral compute scratch lives in the sandbox.

File I/O is proxied over the sandbox's authenticated `/execute` HTTP API:
`execute()`/`aexecute()` run shell commands, and `upload_files()`/
`download_files()` move bytes with base64 (see `app.sandbox_fs`). Everything is
scoped to the hardened, non-root, network-isolated container.
"""
from __future__ import annotations

import base64
import binascii
import logging

import httpx
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from app.config import settings
from app.sandbox_fs import NO_OUTPUT, download_command, fs_error_code, upload_command

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
    output = "\n".join(parts) if parts else NO_OUTPUT
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


def _ok(resp: ExecuteResponse) -> bool:
    # `None` means the backend couldn't determine an exit code; treat it as
    # non-fatal (mirrors BaseSandbox.delete, which only fails on a definite
    # non-zero) so a status-less success isn't reported as an error.
    return resp.exit_code in (0, None)


def _upload_response(path: str, resp: ExecuteResponse) -> FileUploadResponse:
    if not _ok(resp):
        return FileUploadResponse(path=path, error=fs_error_code(resp.output))
    return FileUploadResponse(path=path)


def _download_response(path: str, resp: ExecuteResponse) -> FileDownloadResponse:
    if not _ok(resp):
        return FileDownloadResponse(path=path, content=None, error=fs_error_code(resp.output))
    if resp.output.strip() in ("", NO_OUTPUT):  # empty file
        return FileDownloadResponse(path=path, content=b"")
    try:
        content = base64.b64decode(resp.output)
    except (binascii.Error, ValueError) as exc:
        return FileDownloadResponse(path=path, content=None, error=f"decode_failed: {exc}")
    return FileDownloadResponse(path=path, content=content)


class SandboxBackend(BaseSandbox):
    """Sandbox `execute` + a filesystem derived from it, both proxied to the
    isolated `sandbox` container. File tools and `execute` share one real
    `/workspace`; `/memories/` and `/skills/` are routed elsewhere in `agent.py`.
    """

    @property
    def id(self) -> str:
        return settings.sandbox_url

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

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [_upload_response(path, self.execute(upload_command(path, content)))
                for path, content in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for path, content in files:
            out.append(_upload_response(path, await self.aexecute(upload_command(path, content))))
        return out

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [_download_response(path, self.execute(download_command(path))) for path in paths]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        out: list[FileDownloadResponse] = []
        for path in paths:
            out.append(_download_response(path, await self.aexecute(download_command(path))))
        return out
