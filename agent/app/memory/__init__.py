"""Memory backend factory.

Selects the LangGraph Store that powers per-user long-term memory and skills,
based on MEMORY_BACKEND. All options provide semantic (vector) search:

  pgvector  (default) : reuses the existing Postgres; best TCO, transactional,
                        scales to tens of millions of vectors.
  qdrant              : dedicated Rust vector DB; best filtered/ANN scaling.
  inmemory            : ephemeral; for local dev/testing only (not persistent).

Swap by setting MEMORY_BACKEND in .env and restarting the agent. See README
"Choosing a memory backend" for the rationale behind the pgvector default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class MemoryStore:
    store: Any
    aclose: Optional[Any] = None  # optional async teardown coroutine-fn


def _index_config(embeddings) -> dict:
    return {
        "dims": settings.embeddings_dims,
        "embed": embeddings,
        # Our memory/skill files are stored as {"content": "..."}.
        "fields": ["content"],
    }


async def build_memory_store(pool, embeddings) -> MemoryStore:
    backend = settings.memory_backend
    log.info("Memory backend: %s", backend)

    if backend == "pgvector":
        from langgraph.store.postgres.aio import AsyncPostgresStore

        store = AsyncPostgresStore(pool, index=_index_config(embeddings))
        await store.setup()
        return MemoryStore(store=store)

    if backend == "inmemory":
        from langgraph.store.memory import InMemoryStore

        store = InMemoryStore(index=_index_config(embeddings))
        return MemoryStore(store=store)

    if backend == "qdrant":
        from qdrant_client import AsyncQdrantClient

        from app.memory.qdrant_store import QdrantStore

        client = AsyncQdrantClient(url=settings.qdrant_url)
        store = QdrantStore(
            client=client,
            embeddings=embeddings,
            collection=settings.qdrant_collection,
            dims=settings.embeddings_dims,
        )
        try:
            await store.setup()
        except Exception as exc:  # noqa: BLE001 - turn a raw error into guidance
            raise RuntimeError(
                f"MEMORY_BACKEND=qdrant but Qdrant is unreachable at "
                f"{settings.qdrant_url} ({exc}). The Qdrant container is not "
                "started by default. Fix with ONE of:\n"
                "  • run:  make up-qdrant        (starts the qdrant profile)\n"
                "  • or:   docker compose --profile qdrant up -d\n"
                "  • or set MEMORY_BACKEND=pgvector in .env (no extra container)."
            ) from exc
        return MemoryStore(store=store, aclose=store.aclose)

    raise RuntimeError(f"Unknown MEMORY_BACKEND: {backend!r}")
