# Convenience wrapper around docker compose. `make help` lists targets.
.DEFAULT_GOAL := help
COMPOSE := docker compose

# Detect the selected memory backend from .env (default pgvector) so we can
# guide you in real time when a target/backend combination is wrong.
MEMORY_BACKEND := $(strip $(shell grep -E '^MEMORY_BACKEND=' .env 2>/dev/null | tail -1 | cut -d= -f2-))
ifeq ($(MEMORY_BACKEND),)
MEMORY_BACKEND := pgvector
endif

# When qdrant is the backend, auto-enable its compose profile for EVERY target
# so `make up` / `make smoke` "just work" without a special command.
ifeq ($(MEMORY_BACKEND),qdrant)
export COMPOSE_PROFILES := qdrant
endif

.PHONY: help up down build rebuild logs logs-agent ps restart pull \
        config secrets nuke sh-agent sh-sandbox psql search-test \
        dashboard up-qdrant stats smoke preflight

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

preflight: ## Check .env + backend/target sanity and print what will happen
	@test -f .env || { echo "ERROR: no .env found. Run:  cp .env.example .env"; exit 1; }
	@echo "→ memory backend: $(MEMORY_BACKEND)"
ifeq ($(MEMORY_BACKEND),qdrant)
	@echo "→ qdrant selected: auto-enabling the 'qdrant' compose profile (COMPOSE_PROFILES=qdrant)."
endif

up: preflight ## Build (if needed) and start the whole stack in the background
	$(COMPOSE) up -d --build

down: ## Stop and remove containers (keeps volumes / data)
	$(COMPOSE) --profile qdrant down

build: ## Build images
	$(COMPOSE) build

rebuild: ## Rebuild images from scratch (no cache)
	$(COMPOSE) build --no-cache

logs: ## Tail logs for all services
	$(COMPOSE) logs -f --tail=100

logs-agent: ## Tail just the agent logs
	$(COMPOSE) logs -f --tail=200 agent

ps: ## Show service status
	$(COMPOSE) ps

restart: preflight ## Restart the agent (after code/prompt changes)
ifeq ($(MEMORY_BACKEND),qdrant)
	$(COMPOSE) up -d qdrant
endif
	$(COMPOSE) up -d --build agent

pull: ## Pull the latest third-party images (searxng, valkey, tor, postgres)
	$(COMPOSE) pull

config: ## Render the fully-interpolated compose config
	$(COMPOSE) config

secrets: ## Print two fresh random secrets for SEARXNG_SECRET / SANDBOX_TOKEN
	@echo "SEARXNG_SECRET=$$(openssl rand -hex 32)"
	@echo "SANDBOX_TOKEN=$$(openssl rand -hex 32)"

sh-agent: ## Open a shell in the agent container
	$(COMPOSE) exec agent bash

sh-sandbox: ## Open a shell in the sandbox container
	$(COMPOSE) exec sandbox bash

psql: ## Open psql against the persistence database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-deepagent} -d $${POSTGRES_DB:-deepagent}

search-test: ## Hit SearXNG directly to confirm search works
	curl -s 'http://127.0.0.1:8888/search?q=hello&format=json' | head -c 800; echo

dashboard: ## Print the local dashboard URL
	@echo "Dashboard: http://127.0.0.1:8899"

up-qdrant: ## Start the stack using the Qdrant memory backend
ifneq ($(MEMORY_BACKEND),qdrant)
	@echo "WARNING: MEMORY_BACKEND=$(MEMORY_BACKEND) in .env (not 'qdrant')."
	@echo "         Qdrant will start, but the agent will keep using $(MEMORY_BACKEND)."
	@echo "         To actually use Qdrant: set MEMORY_BACKEND=qdrant in .env, then 'make restart'."
	@echo ""
endif
	$(COMPOSE) --profile qdrant up -d --build

stats: ## Curl the dashboard stats JSON
	curl -s http://127.0.0.1:8899/api/stats | head -c 1200; echo

smoke: preflight ## Readiness self-check (DB, embeddings, search, sandbox) — no bot needed
ifeq ($(MEMORY_BACKEND),qdrant)
	@echo "→ starting qdrant first so the memory-store check can reach it…"
	$(COMPOSE) up -d qdrant
endif
	$(COMPOSE) build agent sandbox
	$(COMPOSE) run --rm agent python -m app.smoke

nuke: ## DESTRUCTIVE: stop and delete containers AND all volumes/data
	$(COMPOSE) --profile qdrant down -v
