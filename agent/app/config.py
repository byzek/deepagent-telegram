"""Centralized configuration read from environment variables.

Everything the app needs is resolved here once at import time so the rest of
the codebase never touches os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            raise ValueError(f"Expected numeric user id, got: {chunk!r}")
    return frozenset(ids)


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # --- LLM -----------------------------------------------------------------
    llm_base_url: str = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8080/v1")
    llm_model: str = os.environ.get("LLM_MODEL", "local-model")
    llm_api_key: str = os.environ.get("LLM_API_KEY", "") or "not-needed"
    llm_temperature: float = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
    llm_max_tokens: int = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
    llm_request_timeout: float = float(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))

    # --- Embeddings (for semantic memory) ------------------------------------
    # Default to the same private endpoint as the chat model.
    embeddings_base_url: str = (
        os.environ.get("EMBEDDINGS_BASE_URL")
        or os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8080/v1")
    )
    embeddings_model: str = os.environ.get("EMBEDDINGS_MODEL", "text-embedding-3-small")
    embeddings_api_key: str = (
        os.environ.get("EMBEDDINGS_API_KEY")
        or os.environ.get("LLM_API_KEY", "")
        or "not-needed"
    )
    embeddings_dims: int = int(os.environ.get("EMBEDDINGS_DIMS", "1536"))

    # --- Memory backend ------------------------------------------------------
    # pgvector (default) | qdrant | inmemory
    memory_backend: str = os.environ.get("MEMORY_BACKEND", "pgvector").strip().lower()
    memory_search_limit: int = int(os.environ.get("MEMORY_SEARCH_LIMIT", "5"))
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    qdrant_collection: str = os.environ.get("QDRANT_COLLECTION", "agent_memory")

    # --- Messaging -----------------------------------------------------------
    # Comma list; empty = auto-enable any platform whose token is present.
    messaging_platforms: tuple[str, ...] = field(
        default_factory=lambda: _split_csv(os.environ.get("MESSAGING_PLATFORMS", ""))
    )
    telegram_bot_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    allowed_telegram_user_ids: frozenset[int] = field(
        default_factory=lambda: _split_ids(os.environ.get("ALLOWED_TELEGRAM_USER_IDS", ""))
    )
    discord_bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    allowed_discord_user_ids: frozenset[int] = field(
        default_factory=lambda: _split_ids(os.environ.get("ALLOWED_DISCORD_USER_IDS", ""))
    )

    # --- Persistence ---------------------------------------------------------
    database_url: str = os.environ.get("DATABASE_URL", "")

    # --- Search --------------------------------------------------------------
    searxng_url: str = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
    brave_api_key: str = os.environ.get("BRAVE_API_KEY", "")

    # --- Sandbox -------------------------------------------------------------
    sandbox_url: str = os.environ.get("SANDBOX_URL", "http://sandbox:8000").rstrip("/")
    sandbox_token: str = os.environ.get("SANDBOX_TOKEN", "")
    sandbox_timeout: int = int(os.environ.get("SANDBOX_TIMEOUT", "120"))

    # --- Metrics -------------------------------------------------------------
    net_sample_interval: float = float(os.environ.get("NET_SAMPLE_INTERVAL", "5"))
    net_metrics_retention_hours: int = int(os.environ.get("NET_METRICS_RETENTION_HOURS", "72"))

    # --- Misc ----------------------------------------------------------------
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    agent_name: str = os.environ.get("AGENT_NAME", "deepagent")

    # --- Derived helpers -----------------------------------------------------
    def enabled_platforms(self) -> list[str]:
        """Which chat platforms to start. Explicit list wins; otherwise any
        platform with a token configured is auto-enabled."""
        if self.messaging_platforms:
            return [p.lower() for p in self.messaging_platforms]
        auto: list[str] = []
        if self.telegram_bot_token:
            auto.append("telegram")
        if self.discord_bot_token:
            auto.append("discord")
        return auto

    def allowed_user_ids(self, platform: str) -> frozenset[int]:
        return {
            "telegram": self.allowed_telegram_user_ids,
            "discord": self.allowed_discord_user_ids,
        }.get(platform, frozenset())

    def validate(self) -> None:
        missing: list[str] = []
        if not self.database_url:
            missing.append("DATABASE_URL")
        if not self.sandbox_token:
            missing.append("SANDBOX_TOKEN")

        platforms = self.enabled_platforms()
        if not platforms:
            missing.append("a messaging token (TELEGRAM_BOT_TOKEN or DISCORD_BOT_TOKEN)")
        if "telegram" in platforms:
            if not self.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.allowed_telegram_user_ids:
                missing.append("ALLOWED_TELEGRAM_USER_IDS")
        if "discord" in platforms:
            if not self.discord_bot_token:
                missing.append("DISCORD_BOT_TOKEN")
            if not self.allowed_discord_user_ids:
                missing.append("ALLOWED_DISCORD_USER_IDS")

        if self.memory_backend not in {"pgvector", "qdrant", "inmemory"}:
            raise RuntimeError(
                f"MEMORY_BACKEND must be pgvector|qdrant|inmemory, got {self.memory_backend!r}"
            )
        if missing:
            raise RuntimeError("Missing required configuration: " + ", ".join(missing))


settings = Settings()
