"""Minimal, dependency-free code-execution service.

Runs shell commands inside THIS container (the isolation boundary) and returns
stdout/stderr/exit code as JSON. Only callers presenting the shared
`X-Sandbox-Token` header are served, so nothing on the network except the agent
can drive it.

This is intentionally an RCE endpoint — that is what a sandbox is. Safety comes
from the container: non-root, no host mounts, dropped capabilities, read-only
root filesystem, and CPU/memory/pids limits (see docker-compose.yml).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("sandbox")

TOKEN = os.environ.get("SANDBOX_TOKEN", "")
TIMEOUT = int(os.environ.get("SANDBOX_TIMEOUT", "120"))
MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT", "3600"))
WORKDIR = os.environ.get("SANDBOX_WORKDIR", "/workspace")
MAX_OUTPUT = 60_000  # chars per stream returned to the model


def _read_clipped(f) -> str:
    size = f.seek(0, os.SEEK_END)
    f.seek(0)
    s = f.read(MAX_OUTPUT).decode(errors="replace")
    if size > MAX_OUTPUT:
        s += f"\n...[truncated {size - MAX_OUTPUT} chars]"
    return s


def _run(command: str, timeout: int | None = None) -> dict:
    timeout = min(timeout or TIMEOUT, MAX_TIMEOUT)
    os.makedirs(WORKDIR, exist_ok=True)
    # Files, not pipes: a backgrounded process inheriting stdout must not keep
    # us waiting after the shell itself has exited.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,  # own process group so we can kill children
            env={**os.environ, "HOME": WORKDIR, "PIP_NO_INPUT": "1"},
        )
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        return {
            "exit_code": proc.returncode,
            "stdout": _read_clipped(out),
            "stderr": _read_clipped(err),
            "timed_out": timed_out,
        }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/execute":
            self._json(404, {"error": "not found"})
            return

        supplied = self.headers.get("X-Sandbox-Token", "")
        if not TOKEN or not hmac.compare_digest(supplied, TOKEN):
            self._json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            command = data["command"]
            timeout = int(data["timeout"]) if data.get("timeout") else None
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"error": f"bad request: {exc}"})
            return

        log.info("execute (timeout=%s): %s", timeout, command[:200])
        self._json(200, _run(command, timeout))

    def log_message(self, *args) -> None:  # silence default access logging
        pass


def main() -> None:
    if not TOKEN:
        raise SystemExit("SANDBOX_TOKEN is required")
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    log.info("Sandbox exec server listening on :8000 (workdir=%s)", WORKDIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
