"""Embeddings client for semantic memory.

Points at an OpenAI-compatible /v1/embeddings endpoint. By default this is the
same private endpoint that serves the chat model.
"""
from __future__ import annotations

import logging

from langchain_openai import OpenAIEmbeddings

from app.config import settings

log = logging.getLogger(__name__)


def build_embeddings() -> OpenAIEmbeddings:
    log.info(
        "Embeddings: model=%s base_url=%s dims=%s",
        settings.embeddings_model,
        settings.embeddings_base_url,
        settings.embeddings_dims,
    )
    return OpenAIEmbeddings(
        model=settings.embeddings_model,
        base_url=settings.embeddings_base_url,
        api_key=settings.embeddings_api_key,
        # Some local servers reject `dimensions`; the store still works because
        # we set the pgvector/qdrant vector size from EMBEDDINGS_DIMS explicitly.
        check_embedding_ctx_length=False,
    )
