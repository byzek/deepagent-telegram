"""A lean LangGraph BaseStore backed by Qdrant.

Implements the async operations that Deep Agents actually uses: get, put,
delete, semantic search, and namespace listing. Namespaces are matched exactly
(which is what the harness does), not by arbitrary prefix.

Each stored item becomes one Qdrant point:
  id      = uuid5(namespace + key)                (stable, upsert-friendly)
  vector  = embedding of the item's text content
  payload = {ns, namespace, key, value, created_at, updated_at}
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)
from qdrant_client import AsyncQdrantClient, models

log = logging.getLogger(__name__)

_NS_UUID = uuid.UUID("6f0b8f9e-0000-4000-8000-000000000000")


def _text_of(value: dict) -> str:
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return value["content"]
    return json.dumps(value, default=str)


def _ns_str(namespace: tuple[str, ...]) -> str:
    return "/".join(namespace)


def _point_id(namespace: tuple[str, ...], key: str) -> str:
    return str(uuid.uuid5(_NS_UUID, _ns_str(namespace) + "\x00" + key))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(raw) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


class QdrantStore(BaseStore):
    def __init__(self, client: AsyncQdrantClient, embeddings, collection: str, dims: int):
        self._client = client
        self._embeddings = embeddings
        self._collection = collection
        self._dims = dims

    # -- lifecycle ------------------------------------------------------------
    async def setup(self) -> None:
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._dims, distance=models.Distance.COSINE
                ),
            )
            log.info("Created Qdrant collection %s (dims=%s)", self._collection, self._dims)
        try:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name="ns",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception:  # noqa: BLE001 - already exists
            pass

    async def aclose(self) -> None:
        await self._client.close()

    # -- helpers --------------------------------------------------------------
    def _ns_filter(self, namespace: tuple[str, ...]) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="ns", match=models.MatchValue(value=_ns_str(namespace))
                )
            ]
        )

    def _to_item(self, payload: dict, *, score: float | None = None):
        namespace = tuple(payload.get("namespace", []))
        common = dict(
            namespace=namespace,
            key=payload.get("key", ""),
            value=payload.get("value", {}),
            created_at=_parse_dt(payload.get("created_at")),
            updated_at=_parse_dt(payload.get("updated_at")),
        )
        if score is not None:
            return SearchItem(**common, score=score)
        return Item(**common)

    # -- op handlers ----------------------------------------------------------
    async def _do_get(self, op: GetOp):
        pid = _point_id(op.namespace, op.key)
        points = await self._client.retrieve(
            self._collection, ids=[pid], with_payload=True, with_vectors=False
        )
        if not points:
            return None
        return self._to_item(points[0].payload)

    async def _do_put(self, op: PutOp) -> None:
        pid = _point_id(op.namespace, op.key)
        if op.value is None:
            await self._client.delete(
                self._collection,
                points_selector=models.PointIdsList(points=[pid]),
            )
            return
        vector = await self._embeddings.aembed_query(_text_of(op.value))
        existing = await self._client.retrieve(
            self._collection, ids=[pid], with_payload=True, with_vectors=False
        )
        created_at = existing[0].payload.get("created_at") if existing else _now()
        payload = {
            "ns": _ns_str(op.namespace),
            "namespace": list(op.namespace),
            "key": op.key,
            "value": op.value,
            "created_at": created_at,
            "updated_at": _now(),
        }
        await self._client.upsert(
            self._collection,
            points=[models.PointStruct(id=pid, vector=vector, payload=payload)],
        )

    async def _do_search(self, op: SearchOp) -> list[SearchItem]:
        ns_filter = self._ns_filter(op.namespace_prefix)
        if op.query:
            vector = await self._embeddings.aembed_query(op.query)
            res = await self._client.query_points(
                self._collection,
                query=vector,
                query_filter=ns_filter,
                limit=op.limit,
                offset=op.offset,
                with_payload=True,
            )
            return [self._to_item(p.payload, score=p.score) for p in res.points]
        # scroll's `offset` is a point-id cursor, not an int count — omit it.
        points, _ = await self._client.scroll(
            self._collection,
            scroll_filter=ns_filter,
            limit=op.limit + (op.offset or 0),
            with_payload=True,
        )
        points = points[op.offset :] if op.offset else points
        return [self._to_item(p.payload, score=None) for p in points]

    async def _do_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        seen: set[tuple[str, ...]] = set()
        offset = None
        while True:
            points, offset = await self._client.scroll(
                self._collection,
                limit=256,
                offset=offset,
                with_payload=True,
            )
            for p in points:
                seen.add(tuple(p.payload.get("namespace", [])))
            if offset is None:
                break
        return sorted(seen)[: op.limit]

    # -- BaseStore interface --------------------------------------------------
    async def abatch(self, ops):
        results = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(await self._do_get(op))
            elif isinstance(op, PutOp):
                results.append(await self._do_put(op))
            elif isinstance(op, SearchOp):
                results.append(await self._do_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await self._do_list_namespaces(op))
            else:  # pragma: no cover - future op types
                raise NotImplementedError(f"Unsupported op: {type(op).__name__}")
        return results

    def batch(self, ops):  # pragma: no cover - this store is async-only
        raise NotImplementedError("QdrantStore is async-only; use abatch/a* methods.")
