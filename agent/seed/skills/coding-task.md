# Skill: Coding / scripting task

Use when the user asks you to write, run, or debug code, or process data.

## Steps
1. Work in `/workspace` inside the sandbox (it persists across conversations).
2. Write files with `execute` (e.g. a heredoc or `cat > file`), then run them.
3. Install Python deps with `pip install --user <pkg>` as needed.
4. Show the user only the relevant output, not full logs.
5. Save reusable artifacts (scripts, datasets, reports) under `/workspace/` and
   tell the user the path so later conversations can pick them up.

## Notes
- The sandbox is unprivileged and network-capable but isolated from the host.
- If a command hangs it is killed at the timeout; break long work into steps.
