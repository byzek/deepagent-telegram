# deepagent-telegram

A self-hosted [Deep Agent](https://docs.langchain.com/oss/python/deepagents/going-to-production)
you talk to over **Telegram and/or Discord**, running entirely in Docker Compose
against your **private OpenAI-compatible LLM**. It has persistent per-user memory
and skills with **semantic (vector) recall**, a **swappable memory backend**
(pgvector / Qdrant / in-memory), private web search via **SearXNG** (with a
**Brave Search API** fallback), an isolated **sandbox** for running code, and a
**local metrics dashboard**.

No LangSmith cloud, no public inbound ports, no Node.js.

---

## Why this shape (and one honest caveat)

The linked LangChain doc pushes their **managed cloud** (LangSmith Deployments)
as the "recommended path" — it hands you threads, a checkpointer, and a store for
free. This project deliberately does **not** use it: everything is self-hosted so
your data and your private model never leave your box. The trade-off is that the
persistence, messaging, search, and sandbox glue that the cloud would provide is
implemented here as ~500 lines of code and a compose file. It's straightforward,
but it's yours to run and maintain.

Design decisions baked in (and why):

| Area | Choice | Rationale |
|---|---|---|
| Messaging | **Telegram + Discord**, long-polling / gateway | Free, no business account, pure-Python, no public HTTPS/TLS needed on day one. Each platform is one swappable module; run either or both. |
| LLM | OpenAI-compatible at `host.docker.internal:8080` | Your private deployment. (`host.docker.local`, which you wrote, isn't a real Docker DNS name.) |
| Persistence | **Postgres** checkpointer + store | Durable conversations + cross-thread memory/skills that survive restarts. |
| Semantic memory | **pgvector** default (swappable) | Vector recall of memories/skills. pgvector reuses Postgres you already run; see [Memory backends](#memory-backends--semantic-search). |
| Memory scope | **Per user, platform-qualified**, allowlist-gated | Each authorized user gets private memory/skills (`telegram:123` ≠ `discord:123`); everyone else is ignored. |
| Observability | **Local dashboard** on `127.0.0.1:8899` | Per-user / per-backend prompts, tokens, tokens/sec + container network. |
| Search | **SearXNG** primary, **Brave API** fallback | SearXNG is private but gets rate-limited/CAPTCHA'd upstream; the Brave API is CAPTCHA-proof. |
| CAPTCHA | Brave API + optional Tor | See [CAPTCHA / rate-limit handling](#captcha--rate-limit-handling). |
| Code exec | Isolated **sandbox** container | Non-root, no host access, resource-limited; the agent never runs code in its own process. |

---

## Architecture

```
                     ┌──────────────┐
   Telegram  <──────►│    agent     │  Deep Agent + Telegram bot (long-polling)
   (you)             │  (no ports)  │
                     └──┬───┬───┬───┘
                        │   │   │
   your private LLM ◄───┘   │   │        (http://host.docker.internal:8080/v1)
                            │   │
              ┌─────────────┘   └─────────────┐
              ▼                               ▼
       ┌────────────┐                  ┌────────────┐
       │  postgres  │  checkpointer    │  sandbox   │  isolated shell/code exec
       │            │  + store (memory)│            │  (/workspace persists)
       └────────────┘                  └────────────┘
              ▲
        ┌─────┴─────┐        ┌────────┐        ┌──────┐
        │  searxng  │◄──────►│ valkey │        │ tor  │ (optional SOCKS proxy)
        │  (search) │        │(cache) │        └──────┘
        └───────────┘        └────────┘
```

All services share one internal bridge network. The only host-published port is
the **SearXNG debug UI on `127.0.0.1:8888`** (localhost only; comment it out in
`docker-compose.yml` to seal it completely). The agent has **no inbound ports** —
it reaches Telegram outbound via long-polling.

---

## Repository layout

```
deepagent-telegram/
├── docker-compose.yml          # the whole stack
├── .env.example                # copy to .env and fill in
├── Makefile                    # make up / logs / secrets / ...
├── README.md
├── agent/                      # the brain
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── seed/                   # starter skills + memories (copied into image)
│   │   ├── memories/instructions.md
│   │   └── skills/*.md
│   └── app/
│       ├── main.py             # entrypoint: builds runtime, starts adapters
│       ├── core.py             # assembles persistence + memory + agent + metrics
│       ├── config.py           # all env vars resolved here
│       ├── persistence.py      # Postgres pool + checkpointer
│       ├── embeddings.py       # OpenAI-compatible embeddings client
│       ├── memory/             # SWAPPABLE memory backend
│       │   ├── __init__.py     #   factory: pgvector | qdrant | inmemory
│       │   └── qdrant_store.py #   custom LangGraph BaseStore for Qdrant
│       ├── agent.py            # Deep Agent construction (model, backend, tools)
│       ├── prompt.py           # system prompt
│       ├── seed.py             # idempotent per-user seeding
│       ├── threads.py          # per-chat conversation tracking (Postgres)
│       ├── metrics.py          # LLM token/speed callback + network sampler
│       ├── tools/
│       │   ├── search.py       # web_search (SearXNG) + brave_search
│       │   ├── sandbox.py      # execute -> sandbox service
│       │   └── memory.py       # search_memory / search_skills (semantic)
│       └── messaging/
│           ├── dispatch.py     # transport-agnostic turn handling + metrics
│           ├── telegram_adapter.py
│           └── discord_adapter.py    # add/replace adapters here
├── dashboard/                  # local metrics UI (FastAPI, no Node)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py
├── sandbox/                    # isolated code execution
│   ├── Dockerfile
│   └── exec_server.py          # tiny token-gated exec service (stdlib only)
└── searxng/
    └── settings.yml            # SearXNG config
```

---

## Prerequisites

- Docker + Docker Compose v2 (`docker compose`, not `docker-compose`).
- Your private LLM reachable from the host at `http://host.docker.internal:8080/v1`
  (OpenAI-compatible: `GET /v1/models`, `POST /v1/chat/completions`).
- A Telegram account.

---

## Setup (first run)

### 1. Create a Telegram bot
1. Open Telegram, talk to [`@BotFather`](https://t.me/BotFather).
2. `/newbot`, follow prompts, copy the **bot token**.
3. Get your **numeric user id** from [`@userinfobot`](https://t.me/userinfobot).

### 2. Configure `.env`
```bash
cd deepagent-telegram
cp .env.example .env
make secrets   # prints fresh SEARXNG_SECRET + SANDBOX_TOKEN to paste in
```
Now edit `.env` and set at minimum:

| Variable | What to put |
|---|---|
| `LLM_MODEL` | **The model id your endpoint serves** (you didn't specify one). Check `GET /v1/models`. |
| `LLM_API_KEY` | Your endpoint's key, or leave blank if it needs none. |
| `EMBEDDINGS_MODEL` | **The embedding model your endpoint serves** (for semantic memory). |
| `EMBEDDINGS_DIMS` | That model's output dimension (e.g. 1536). Must match exactly. |
| `TELEGRAM_BOT_TOKEN` | From BotFather. |
| `ALLOWED_TELEGRAM_USER_IDS` | Your numeric id(s), comma-separated. |
| `POSTGRES_PASSWORD` | A strong password. |
| `SEARXNG_SECRET` | From `make secrets`. |
| `SANDBOX_TOKEN` | From `make secrets`. |
| `BRAVE_API_KEY` | Optional but recommended — enables the CAPTCHA-proof fallback. |
| Discord (optional) | See [Discord setup](#discord-setup) for `DISCORD_BOT_TOKEN` + `ALLOWED_DISCORD_USER_IDS`. |

### 3. Verify (recommended)
Before wiring a bot, run the readiness self-check. It boots Postgres + sandbox +
SearXNG and exercises the real dependencies — DB/tables, the memory store, an
**embeddings + semantic-recall round-trip**, sandbox exec, and LLM reachability —
without needing any messaging token:
```bash
make smoke
```
Every row should read `PASS` (the LLM row is a `WARN`-only reachability probe).
Fix any `FAIL` before continuing. The check starts its dependencies and leaves
them running — `make down` to stop, or just continue to `make up`. The Makefile
reads `MEMORY_BACKEND` from your `.env` and auto-handles the rest: if you set
`MEMORY_BACKEND=qdrant`, `make smoke`/`make up` enable the Qdrant profile and
start the container for you (and tell you they're doing it). If Qdrant is ever
unreachable, the failing row prints the exact command to fix it.

### 4. Launch
```bash
make up          # builds images and starts everything
make logs-agent  # watch it come online
```
When you see `Started adapters: [...]`, message your bot. Try: `/start`, then
"search the web for the latest on X and summarize." Open the metrics dashboard at
**http://127.0.0.1:8899**.

---

## Everyday use

Send the bot normal messages. Built-in commands:

- `/start` — greeting
- `/help` — capabilities
- `/whoami` — shows your Telegram id and whether you're authorized
- `/reset` — start a fresh conversation (clears short-term context; your
  long-term memory and skills are kept)

Make targets:

```bash
make up          # start / update
make logs        # tail all logs
make logs-agent  # tail agent only
make restart     # rebuild + restart agent after editing code or the prompt
make ps          # status
make down        # stop (keeps data)
make nuke        # DESTRUCTIVE: stop + delete all volumes/data
make psql        # open a psql shell on the store
make search-test # verify SearXNG returns JSON
make stats       # curl the dashboard stats JSON
make up-qdrant   # start using the Qdrant memory backend
make smoke       # readiness self-check (no bot needed)
```

Backend images are built with **[uv](https://docs.astral.sh/uv/)** (fast,
reproducible, Node-free) — no manual venv or pip steps required.

Dashboard: **http://127.0.0.1:8899** (also `make dashboard`).

---

## How persistence works (memory & skills)

Two Postgres-backed layers, both created automatically on first boot:

1. **Checkpointer** — conversation state per `thread_id` (`tg:<chat>:<epoch>`).
   Follow-ups continue the same thread; `/reset` rotates the epoch.
2. **Store** — cross-thread long-term data, namespaced **per user**:
   - `/memories/` → store namespace `("memories", <user_id>)`
   - `/skills/`   → store namespace `("skills", <user_id>)`

The agent reads `/memories/instructions.md` at the start of each conversation and
can write updates back. Skills are reusable playbooks under `/skills/`. On
startup, the files in `agent/seed/` are seeded into **each allowed user's**
namespace **idempotently** — existing files are never overwritten, so user edits
persist across restarts and image rebuilds.

Data lives in the `pgdata` Docker volume. It survives `make down`; only `make
nuke` deletes it.

To add or change starter skills, edit files under `agent/seed/skills/`, then
`make restart`. (New seed files appear for users who don't already have one with
that name.)

---

## CAPTCHA / rate-limit handling

You asked for "both" — here's what's wired and why.

**1. Brave Search API (the real answer).** Set `BRAVE_API_KEY` and the agent gets
a `brave_search` tool. It's a proper API, so it never sees a CAPTCHA. The system
prompt tells the agent to try SearXNG first and fall back to Brave when SearXNG is
empty or rate-limited. This is the reliable path — prefer it.

**2. Tor proxy (situational, off by default).** A `tor` container is included and
healthchecked. You can route SearXNG's upstream engine traffic through it by
uncommenting the `proxies:` block in `searxng/settings.yml`, then `make restart`
(or restart the `searxng` service).

> Honest caveat: routing **all** engines through Tor usually makes things *worse*
> — Google/Bing actively block Tor exit nodes, producing more CAPTCHAs and fewer
> results. Tor only helps when **your own IP** is what's blocked. For everyday
> CAPTCHA avoidance, use the Brave fallback. Tor is there for the specific case
> where you need to change your apparent source IP.

Other levers in `searxng/settings.yml`: enable/disable specific engines, and
`server.limiter: false` (already set) so the instance never throttles the agent.

---

## Sandbox (code execution)

The `execute` tool runs shell commands in the `sandbox` container, working
directory `/workspace` (persisted in the `sandbox_workspace` volume). It's
locked down: non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root
filesystem (writable `/workspace` + tmpfs `/tmp`), and CPU/memory/pids limits.

- Python package installs: `pip install --user <pkg>` (unprivileged; no apt at
  runtime). `git`, `curl`, `jq` are preinstalled.
- To add system packages, edit `sandbox/Dockerfile` and rebuild.
- The exec service is gated by `SANDBOX_TOKEN`, so only the agent can call it.

This is by design an execution endpoint — the *container* is the security
boundary. Keep it on the internal network (it is) and keep the token secret.

---

## Memory backends & semantic search

Every conversation can recall relevant past memories/skills **by meaning**, not
just by filename. The agent gets two extra tools — `search_memory(query)` and
`search_skills(query)` — that run a vector similarity search over the current
user's namespace. Writes to `/memories/` and `/skills/` are embedded
automatically; retrieval is ranked by cosine similarity.

Embeddings come from your private endpoint's `/v1/embeddings` by default. Set
these in `.env`:

- `EMBEDDINGS_MODEL` — the embedding model your endpoint serves.
- `EMBEDDINGS_DIMS` — its output dimensionality (must match exactly:
  `text-embedding-3-small`=1536, `bge-base`=768, `e5-large`=1024, …).

### Choosing a backend (and why pgvector is the default)

Set `MEMORY_BACKEND` in `.env`. Options:

| Backend | Persistent | Extra infra | Best for |
|---|---|---|---|
| `pgvector` **(default)** | Yes | None (reuses Postgres) | Almost everyone. |
| `qdrant` | Yes | +1 container | Heavy metadata-filtered / very large ANN workloads. |
| `inmemory` | **No** | None | Local dev / testing only. |

The 2026 consensus (and the benchmarks) landed on **pgvector as the default**
for self-hosted agent memory: you already run Postgres here, so it adds *zero*
new infrastructure, keeps embeddings transactionally consistent with the rest of
your data, uses the same backups/monitoring/credentials, and comfortably handles
tens of millions of vectors — which is orders of magnitude beyond what per-user
agent memory needs. Qdrant (Rust, dedicated) wins when filtered search or
horizontal sharding at 1M+ vectors is the *core* workload, at the cost of running
and syncing a second datastore. Chroma is prototyping-only and deliberately not
included. Rule of thumb: **stay on pgvector until a filtering/scale requirement
forces you off it.**

### Swapping

```bash
# pgvector (default) — nothing special
MEMORY_BACKEND=pgvector    # in .env
make restart

# Qdrant — the Makefile starts the optional container for you
MEMORY_BACKEND=qdrant      # in .env
make restart               # detects qdrant, enables its profile, starts it

# InMemory (dev)
MEMORY_BACKEND=inmemory    # in .env
make restart
```

Backends are independent stores. Switching backends does **not** migrate
existing memories — each backend holds its own copy (seeds are re-created on
first boot of a new backend). The dashboard's "per memory backend" panel lets you
compare them (e.g. how much a backend inflates prompt tokens via retrieved
context).

## Discord setup

1. [Discord Developer Portal](https://discord.com/developers/applications) → New
   Application → Bot → copy the **token** → `DISCORD_BOT_TOKEN`.
2. Under the bot settings, enable the **Message Content Intent** (privileged) —
   required for the bot to read message text.
3. Invite the bot to your server (OAuth2 URL, `bot` scope) **or** just DM it.
4. Put your numeric Discord user id(s) in `ALLOWED_DISCORD_USER_IDS` (enable
   Developer Mode in Discord → right-click yourself → Copy User ID).
5. `make restart`.

Behavior: the bot replies to allowlisted users in **DMs**, and in servers only
when **@mentioned**. Commands: `!reset`, `!whoami`, `!help`.

Run one or both platforms via `MESSAGING_PLATFORMS` (blank = auto-enable any
platform whose token is set). User memory is platform-qualified, so the same
person on Telegram and Discord has separate memory unless you deliberately share.

## Dashboard

A read-only metrics UI at **http://127.0.0.1:8899** (localhost-only). It shows:

- **Per user** and **per memory backend** and **per model**: prompt count,
  prompt/completion/total tokens, and average tokens/sec.
- **Network**: the agent container's bytes + packets rx/tx, both totals (since
  container start) and current per-second rates, with a live sparkline.

Honest limitation you accepted: network is **container-level, not per-user**.
Packets aren't tagged with which user caused them, and attributing them would
require mounting the Docker socket (a security no-go, per the project rules). So
token/prompt/speed metrics are per-user and per-backend; network is a single
whole-container panel. The agent samples `/proc/net/dev` every
`NET_SAMPLE_INTERVAL` seconds into Postgres; the dashboard only reads the DB (no
privileged access).

How tokens/sec is measured: a per-turn callback sums *pure generation time* and
completion tokens across every model call in the turn (main agent, tool loop, and
subagents), so the rate reflects generation speed, not wall-clock including tool
time. Requires your endpoint to return `usage` (most OpenAI-compatible servers
do); if it doesn't, token counts read as 0 but everything else still works.

## Swapping the messaging platform

Everything except `agent/app/messaging/telegram_adapter.py` is
transport-agnostic. To move to Signal, Discord, or Slack, write a replacement
adapter that:
1. Authorizes senders against `settings.allowed_user_ids`.
2. Derives a stable `thread_id` per conversation and a `user_id` per person.
3. Calls `agent.ainvoke({"messages": [...]}, config=..., context=Context(user_id=...))`.
4. Sends `result["messages"][-1].content` back (chunked to the platform limit).

Pure-Python options that respect the no-Node constraint: Signal via `signal-cli`
(JVM daemon), Discord via `discord.py`, Slack via `slack_bolt` (Socket Mode, no
public URL). Ask and I'll generate the adapter + compose changes.

---

## Configuration reference

All variables live in `.env` (see `.env.example` for the annotated list).
Highlights:

- **LLM**: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TEMPERATURE`,
  `LLM_MAX_TOKENS`, `LLM_REQUEST_TIMEOUT`.
- **Telegram**: `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`.
- **Postgres**: `POSTGRES_HOST/PORT/DB/USER/PASSWORD` (compose derives
  `DATABASE_URL`).
- **Search**: `SEARXNG_URL`, `SEARXNG_SECRET`, `BRAVE_API_KEY`.
- **Sandbox**: `SANDBOX_URL`, `SANDBOX_TOKEN`, `SANDBOX_TIMEOUT`.
- **Prompt**: `AGENT_SYSTEM_PROMPT` (optional full override), `AGENT_NAME`.

---

## Troubleshooting

**Agent exits with "Missing required configuration".** Fill in the required
`.env` values (bot token, allowed users, DB password, sandbox token).

**Bot doesn't respond.** Check `make logs-agent`. Confirm your id is in
`ALLOWED_TELEGRAM_USER_IDS` (`/whoami` shows it). Unauthorized users are silently
ignored by design.

**LLM connection errors.** From the host, `curl http://localhost:8080/v1/models`.
Inside the agent it's `host.docker.internal:8080`. On Linux this resolves via the
`host-gateway` mapping already in the compose file. If your server binds only to
`127.0.0.1`, make it listen on `0.0.0.0` so containers can reach it.

**`create_deep_agent() got an unexpected keyword argument 'checkpointer'`.**
Your installed `deepagents` is older than this code expects. `docker compose
build --no-cache agent` to pull the pinned range in `requirements.txt`, or bump
the pin. As a fallback, persistence can be attached at compile time instead —
open an issue/ask and I'll adjust `agent/app/agent.py`.

**SearXNG returns HTML, not JSON / 403.** Ensure `search.formats` includes
`json` (it does) and `server.limiter: false` (it is). `make search-test` should
print JSON.

**SearXNG secret not injected.** If your image build doesn't replace
`ultrasecretkey`, set `server.secret_key` in `searxng/settings.yml` directly to a
random 64-hex string and `make restart`.

**Sandbox "unauthorized".** `SANDBOX_TOKEN` must match between the `.env` (agent)
and the sandbox service — they read the same value, so just don't leave it blank.

**pgvector / "type vector does not exist".** The Postgres image must ship
pgvector — this stack uses `pgvector/pgvector:pg16` for that reason. If you
pointed it at a plain `postgres` image, semantic search will fail on setup.

**Embeddings errors / dimension mismatch.** `EMBEDDINGS_MODEL` must be a model
your endpoint actually serves at `/v1/embeddings`, and `EMBEDDINGS_DIMS` must
equal its output size. A wrong dimension surfaces as an insert/search error from
the vector store.

**Qdrant backend won't connect.** Start it: `docker compose --profile qdrant up
-d` (or `make up-qdrant`). It's not launched by default.

**Discord bot silent.** Enable the **Message Content Intent** in the Developer
Portal, confirm your id is in `ALLOWED_DISCORD_USER_IDS` (`!whoami`), and remember
it only replies in DMs or when @mentioned in servers.

**`ImportError: cannot import name 'ToolRuntime'`.** The semantic-search tools
import `ToolRuntime` from `langchain.tools` (per the deepagents docs). If your
installed versions moved it, `docker compose build --no-cache agent`; if it
persists, tell me your `deepagents`/`langchain` versions and I'll adjust the
import.

**Dashboard empty.** It only has data after the agent has answered at least one
message (token rows) and run long enough to take two network samples.

---

## Security notes

- No secrets are committed; `.env` is gitignored. Rotate `SANDBOX_TOKEN`,
  `SEARXNG_SECRET`, and `POSTGRES_PASSWORD` before real use.
- The sandbox is a deliberate code-execution surface; it's unprivileged,
  network-internal, token-gated, and resource-capped. Don't publish its port.
- Access is allowlist-only by Telegram user id. Long-polling means no inbound
  ports and no exposed webhook to secure.
- Per-user memory isolation prevents one user's stored notes from leaking into
  another's context.
