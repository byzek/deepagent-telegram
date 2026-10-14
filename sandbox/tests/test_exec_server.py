"""Tests for the sandbox exec server's process lifecycle (stdlib-only).

Run without pytest:   python3 tests/test_exec_server.py   (from sandbox/)
Run with pytest:      pytest tests/test_exec_server.py
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SANDBOX_WORKDIR"] = tempfile.mkdtemp()
os.environ["SANDBOX_TIMEOUT"] = "5"

import exec_server  # noqa: E402


def test_background_child_holding_stdout_does_not_block():
    start = time.monotonic()
    res = exec_server._run("sleep 30 & echo done")
    assert time.monotonic() - start < 3, "server waited on a backgrounded child"
    assert res["stdout"].strip() == "done"
    assert not res["timed_out"]


def test_output_is_clipped():
    res = exec_server._run(f"head -c {exec_server.MAX_OUTPUT + 10} /dev/zero | tr '\\0' x")
    assert res["stdout"].endswith("[truncated 10 chars]")


def test_timeout_reports_and_returns():
    start = time.monotonic()
    res = exec_server._run("echo partial; sleep 60")
    assert res["timed_out"]
    assert "partial" in res["stdout"]
    assert time.monotonic() - start < exec_server.TIMEOUT + 5


def test_background_process_outlives_command():
    res = exec_server._run("nohup sleep 30 >/dev/null 2>&1 & echo $!")
    pid = int(res["stdout"].strip())
    try:
        time.sleep(0.5)
        os.kill(pid, 0)  # raises if it was killed with the command
    finally:
        os.kill(pid, signal.SIGKILL)


def test_per_call_timeout_overrides_default():
    res = exec_server._run("sleep 7; echo late", timeout=10)
    assert not res["timed_out"]
    assert res["stdout"].strip() == "late"


def test_per_call_timeout_can_shorten():
    start = time.monotonic()
    res = exec_server._run("sleep 30", timeout=1)
    assert res["timed_out"]
    assert time.monotonic() - start < 3


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
