"""Pure shell-command builders for proxying file I/O to the sandbox.

`BaseSandbox` derives every file tool (`ls`/`read_file`/`write_file`/`edit`/
`grep`/`glob`) from two primitives: `execute()` and `upload_files()`. These
helpers implement the byte transfer for those primitives over the sandbox's
`/execute` API, using base64 so arbitrary binary is shell-safe (the same trick
`skills_install.sandbox_write_command` uses for text).

Kept dependency-free (stdlib only, no deepagents) so the transfer logic is
unit-testable without the LangChain/deepagents stack.
"""
from __future__ import annotations

import base64
import shlex

NO_OUTPUT = "<no output>"
"""Sentinel `SandboxBackend._to_response` emits when a command produced no
output. Treated as empty (not an error) by the transfer response mappers."""


def upload_command(path: str, content: bytes) -> str:
    """Shell command that writes raw `content` to `path` in the sandbox,
    creating parent dirs. Content is base64-encoded so arbitrary binary
    survives the shell."""
    blob = base64.b64encode(content).decode("ascii")
    parent = path.rsplit("/", 1)[0] or "/"
    return (
        f"mkdir -p {shlex.quote(parent)} && "
        f"printf %s {shlex.quote(blob)} | base64 -d > {shlex.quote(path)}"
    )


def download_command(path: str) -> str:
    """Shell command that base64-encodes a sandbox file to stdout for transfer."""
    return f"base64 {shlex.quote(path)}"


def fs_error_code(output: str) -> str:
    """Map a failed command's output to a standardized error string.

    Returns a `FileOperationError` literal for recognized shell errors, the
    trimmed output for anything else, or a generic code when there's nothing
    to go on.
    """
    low = output.lower()
    if "no such file" in low or "not found" in low:
        return "file_not_found"
    if "permission denied" in low:
        return "permission_denied"
    if "is a directory" in low:
        return "is_directory"
    trimmed = output.strip()
    if not trimmed or trimmed == NO_OUTPUT:
        return "operation_failed"
    return trimmed
