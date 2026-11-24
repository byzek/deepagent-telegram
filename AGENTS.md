# AGENTS.md

## Tests
- Agent (all, real deps): `cd agent && uv run -p 3.12 --with-requirements requirements.txt --with pytest pytest -q tests`
- Most agent tests are stdlib-only: `cd agent && uv run -p 3.12 --with httpx python tests/test_russian.py`
- Voice service: `cd voice && python3 tests/test_split.py`
- System python is 3.9; use `uv run -p 3.12`. No Node toolchain.

## Layout notes
- Scheduled "special" jobs use sentinel prompts (`practice.SENTINEL`, `russian.SENTINEL`, `russian.GRADE`) handled in `scheduler._fire`.
- New tables: add `ensure_table` + DDL, call it in `core.build_app_context`, cover it in `tests/test_ddl.py`.
- Optional services use compose profiles (`qdrant`, `voice`); enable via `COMPOSE_PROFILES` in `.env`.
- Reflection/feedback lives in `agent/app/reflect.py` (tables `turn_log`, `feedback`, `reflection_log`); `[[reflect]]` jobs run `reflect.run` directly (no agent turn). Tests: `tests/test_reflect.py`.
