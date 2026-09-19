"""Unit tests for the pure sandbox file-transfer helpers (stdlib-only).

Run without pytest:   python3 tests/test_sandbox_fs.py   (from agent/)
Run with pytest:      pytest tests/test_sandbox_fs.py
"""
from __future__ import annotations

import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sandbox_fs import (  # noqa: E402
    NO_OUTPUT,
    download_command,
    fs_error_code,
    upload_command,
)


def _blob(cmd: str) -> str:
    m = re.search(r"printf %s (\S+) \| base64 -d", cmd)
    assert m, f"no base64 payload found in: {cmd!r}"
    return m.group(1).strip("'")


def test_upload_command_roundtrips_binary():
    content = bytes(range(256))  # every byte value, incl. NUL and 0xFF
    cmd = upload_command("/workspace/shots/a.png", content)
    assert "mkdir -p" in cmd
    assert "base64 -d" in cmd
    assert base64.b64decode(_blob(cmd)) == content


def test_upload_command_creates_parent_dir():
    cmd = upload_command("/workspace/a/b/c.txt", b"x")
    assert "mkdir -p /workspace/a/b" in cmd


def test_upload_command_root_level_path():
    cmd = upload_command("/top", b"x")
    assert "mkdir -p / " in cmd


def test_upload_command_quotes_spaces():
    cmd = upload_command("/work space/f.txt", b"hi")
    assert "'/work space/f.txt'" in cmd
    assert base64.b64decode(_blob(cmd)) == b"hi"


def test_download_command():
    assert download_command("/workspace/s h.png") == "base64 '/workspace/s h.png'"


def test_fs_error_code_maps_known_errors():
    assert fs_error_code("base64: /x: No such file or directory") == "file_not_found"
    assert fs_error_code("bash: /x: Permission denied") == "permission_denied"
    assert fs_error_code("base64: /d: Is a directory") == "is_directory"


def test_fs_error_code_passes_through_unknown():
    assert fs_error_code("some novel failure") == "some novel failure"


def test_fs_error_code_empty_is_generic():
    assert fs_error_code("") == "operation_failed"
    assert fs_error_code(NO_OUTPUT) == "operation_failed"


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
