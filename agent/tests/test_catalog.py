"""Unit tests for the tools/skills catalog (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_catalog.py   (from agent/)
Run with pytest:      pytest tests/test_catalog.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.catalog import classify_skills, classify_tools  # noqa: E402

SEED = {"web-research.md": "A", "coding-task.md": "B"}


def test_classify_skills_baked_modified_learned():
    stored = {"web-research.md": "A", "coding-task.md": "B edited", "airtable.md": "X"}
    out = {s["name"]: s["origin"] for s in classify_skills(stored, SEED)}
    assert out == {"web-research.md": "baked", "coding-task.md": "modified",
                   "airtable.md": "learned"}


def test_classify_skills_reports_missing_baked():
    out = {s["name"]: s["origin"] for s in classify_skills({"web-research.md": "A"}, SEED)}
    assert out["coding-task.md"] == "deleted"


def test_classify_tools_groups_sandbox_scripts_by_skill():
    baked = [{"name": "web_search", "description": "d"}]
    files = ["/workspace/skills/pdf/scripts/fill.py", "/workspace/skills/pdf/SKILL.md",
             "/workspace/tools/hello.sh", "/workspace/skills/pdf/run.sh"]
    out = classify_tools(baked, files)
    assert out["baked"] == baked
    assert out["developed"] == [
        {"name": "pdf", "files": ["run.sh", "scripts/fill.py"]},
        {"name": "tools", "files": ["hello.sh"]},
    ]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
