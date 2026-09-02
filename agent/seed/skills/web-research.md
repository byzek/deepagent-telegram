# Skill: Web research

Use when the user asks a question that needs current or external information.

## Steps
1. Call `web_search` (SearXNG) with a focused query.
2. If it returns `NO_RESULTS` or an error, retry once with a reworded query,
   then fall back to `brave_search`.
3. Open the 1–3 most relevant results only if you need detail beyond the
   snippet (fetch via the `execute` sandbox with `curl -sL <url>` if needed).
4. Synthesize a short answer. Always list the source URLs you relied on.

## Notes
- Prefer primary sources and recent pages.
- Don't dump raw search output into the chat — summarize.
