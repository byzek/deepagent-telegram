"""Unit tests for skills export/seed (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_skills_bundle.py   (from agent/)
Run with pytest:      pytest tests/test_skills_bundle.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.skills_bundle import export_bundle, seed_workspace  # noqa: E402


class FakeStore:
    async def asearch(self, namespace, limit=10):
        assert namespace == ("skills", "telegram:1")
        return [SimpleNamespace(key="pdf.md", value={"content": "# pdf"})]


async def fake_cmd(cmd):
    if cmd.startswith("find"):
        return {"stdout": "/workspace/skills/pdf/run.py\n"}
    return {"stdout": base64.b64encode(b"print(1)").decode()}


def test_export_bundle_contains_skills_and_code_only():
    buf = io.BytesIO()
    asyncio.run(export_bundle(FakeStore(), "telegram:1", fake_cmd, buf))
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        names = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    assert names == {"skills/pdf.md": b"# pdf", "workspace/skills/pdf/run.py": b"print(1)"}


def test_seed_workspace_never_overwrites():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "skills/pdf").mkdir(parents=True)
        (Path(d) / "skills/pdf/run.py").write_text("x")
        cmds = []

        async def run(c):
            cmds.append(c)
            return {}

        asyncio.run(seed_workspace(Path(d), run))
    assert len(cmds) == 1
    assert cmds[0].startswith("[ -e /workspace/skills/pdf/run.py ] || ")


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
