# Skill: Network audit

Use when the user asks what you've been connecting to, whether data leaves the
machine, or why something can't reach the network.

## Steps
1. Call `network_connections(hours=24)` for the full picture, or
   `network_connections(external_only=True)` to see only internet peers.
2. Group the results by peer name. Expected internal peers: postgres, searxng,
   sandbox, valkey, qdrant, host (the private LLM). Expected external peers come
   from sandbox work (pip, git, curl) and image generation if it's enabled.
   Telegram/Discord API hosts are expected for the agent.
3. Flag anything external that you can't tie to a task the user asked for.
   Check `first_seen` against what you were doing then.
4. For a live view of right now, run in the sandbox:
   `cat /proc/net/tcp /proc/net/tcp6` (hex addresses) or
   `ss -tunp 2>/dev/null` if available.
5. Report a short summary: peers, counts, anything unexpected. Point the user to
   the dashboard's Connections panel (http://127.0.0.1:8899) for the graph.

## Notes
- SearXNG's own upstream search traffic happens in its container and isn't
  tracked. Only the agent and sandbox are.
- Peaks are concurrent sockets in one sample, not total requests.
