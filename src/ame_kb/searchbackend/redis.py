"""RediSearch HybridIndex: FT full-text channel + KNN vector channel, fused
client-side with the same rrf_merge as the MySQL backend (so SEARCH_BACKEND can
be switched with structurally identical fusion).

One FT index over hash docs keyed `{prefix}:{graph_no}:{graph_version}:{object_type}:{object_no}`:
  graph_no / graph_version / object_type / object_no / workspace_id  as TAG
  searchable_text                                                    as TEXT
  embedding                                                          as VECTOR

Putting graph_no/graph_version in the key (not just the TAG fields) keeps
versions isolated: v1 and v2 of the same object_no live under distinct keys, so
an upsert of one never overwrites the other and a version-scoped delete cannot
take the other version's row with it.

CJK note: Redis Stack tokenizes Chinese via the Friso tokenizer when the query
runs with LANGUAGE chinese; we pass that so full-text recall works on Chinese
text (the main added cost of putting the text channel in Redis). Absolute text
scores differ from MySQL FULLTEXT, so redis|mysql parity is on fusion structure
and ranking, not raw scores.
"""
from __future__ import annotations

from array import array
from typing import List, Optional, Sequence

from ..config import get_settings
from ..embed import embed_texts, embedding_available
from ..rrf import rrf_merge
from .base import HybridIndex, IndexEntry, SearchFilters

# RediSearch TAG / query special characters that must be backslash-escaped.
_SPECIAL = set(",.<>{}[]\"':;!@#$%^&*()-+=~/ \\")


def _esc_tag(value: str) -> str:
    return "".join("\\" + c if c in _SPECIAL else c for c in str(value))


def _vec_bytes(vec: Sequence[float]) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


class RedisHybridIndex(HybridIndex):
    def __init__(self) -> None:
        self._client = None
        self._dim: Optional[int] = None
        self._index_ready = False

    # --- connection / index lifecycle -------------------------------------
    def _redis(self):
        if self._client is None:
            import redis  # imported lazily so mysql-only installs don't need it

            self._client = redis.from_url(
                get_settings().redis_url, decode_responses=False
            )
        return self._client

    def _prefix(self) -> str:
        return get_settings().redis_index_prefix

    def _index_name(self) -> str:
        return f"{self._prefix()}_idx"

    def _doc_key(
        self, graph_no: str, graph_version, object_type: str, object_no: str
    ) -> str:
        return (
            f"{self._prefix()}:{graph_no}:{graph_version}:{object_type}:{object_no}"
        )

    def _resolve_dim(self) -> Optional[int]:
        if self._dim:
            return self._dim
        s = get_settings()
        if s.embed_dim > 0:
            self._dim = s.embed_dim
        elif embedding_available():
            self._dim = len(embed_texts(["dim probe"])[0])
        return self._dim

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        from redis.commands.search.field import TagField, TextField, VectorField

        try:  # redis-py >= 5.1 renamed the module to snake_case
            from redis.commands.search.index_definition import (
                IndexDefinition,
                IndexType,
            )
        except ModuleNotFoundError:  # older redis-py
            from redis.commands.search.indexDefinition import (
                IndexDefinition,
                IndexType,
            )

        client = self._redis()
        name = self._index_name()
        try:
            client.ft(name).info()
            self._index_ready = True
            return
        except Exception:  # noqa: BLE001 - index does not exist yet
            pass

        fields: List = [
            TagField("graph_no"),
            TagField("graph_version"),
            TagField("object_type"),
            TagField("object_no"),
            TagField("workspace_id"),
            TextField("searchable_text"),
        ]
        dim = self._resolve_dim()
        if dim:
            fields.append(
                VectorField(
                    "embedding",
                    "HNSW",
                    {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"},
                )
            )
        definition = IndexDefinition(
            prefix=[f"{self._prefix()}:"], index_type=IndexType.HASH
        )
        client.ft(name).create_index(fields, definition=definition)
        self._index_ready = True

    # --- filter compilation ------------------------------------------------
    def _base_filter(self, filters: SearchFilters) -> str:
        parts = [f"@object_type:{{{_esc_tag(filters.object_type)}}}"]
        if not filters.ignore_graph_scope:
            parts.append(f"@graph_no:{{{_esc_tag(filters.graph_no)}}}")
            parts.append(f"@graph_version:{{{_esc_tag(filters.graph_version)}}}")
        if filters.workspace_ids:
            ws = "|".join(_esc_tag(w) for w in filters.workspace_ids)
            parts.append(f"@workspace_id:{{{ws}}}")
        if filters.restrict_object_nos is not None:
            nos = list(filters.restrict_object_nos)
            if not nos:
                return ""  # empty restrict -> match nothing
            joined = "|".join(_esc_tag(n) for n in nos)
            parts.append(f"@object_no:{{{joined}}}")
        return " ".join(parts)

    # --- channels ----------------------------------------------------------
    def _text_channel(
        self, base: str, query: str, min_score: float, limit: int
    ) -> List[str]:
        from redis.commands.search.query import Query

        if base == "":
            return []
        # Escape query text minimally; keep it as a plain phrase match.
        q_text = query.replace("\\", "\\\\").replace('"', '\\"').strip()
        q_str = f"({base}) {q_text}" if q_text else f"({base})"
        q = (
            Query(q_str)
            .language("chinese")
            .return_fields("object_no")
            .paging(0, limit)
            .with_scores()
            .dialect(2)
        )
        res = self._redis().ft(self._index_name()).search(q)
        out: List[str] = []
        for doc in res.docs:
            if float(doc.score) <= min_score:
                continue
            out.append(_decode(doc.object_no))
        return out

    def _vector_channel(
        self,
        base: str,
        query_embedding: Sequence[float],
        min_score: float,
        limit: int,
    ) -> List[str]:
        from redis.commands.search.query import Query

        if base == "":
            return []
        q_str = f"({base})=>[KNN {limit} @embedding $vec AS vec_score]"
        q = (
            Query(q_str)
            .return_fields("object_no", "vec_score")
            .sort_by("vec_score")
            .paging(0, limit)
            .dialect(2)
        )
        params = {"vec": _vec_bytes(query_embedding)}
        res = self._redis().ft(self._index_name()).search(q, query_params=params)
        out: List[str] = []
        for doc in res.docs:
            # COSINE distance -> similarity; match MySQL's "similarity > min".
            similarity = 1.0 - float(doc.vec_score)
            if similarity <= min_score:
                continue
            out.append(_decode(doc.object_no))
        return out

    # --- HybridIndex API ---------------------------------------------------
    def search(
        self,
        query: str,
        *,
        filters: SearchFilters,
        limit: int,
        min_score_text: float,
        min_score_embedding: float,
        query_embedding: Optional[Sequence[float]] = None,
        warnings: Optional[List[str]] = None,
    ) -> List[str]:
        warnings = warnings if warnings is not None else []
        self._ensure_index()
        base = self._base_filter(filters)
        channels: List[List[str]] = []
        try:
            channels.append(
                self._text_channel(base, query, min_score_text, limit)
            )
        except Exception as exc:  # noqa: BLE001 - one channel is non-fatal
            warnings.append(f"redis fulltext {filters.object_type} failed: {exc}")
        if query_embedding is not None and self._resolve_dim():
            try:
                channels.append(
                    self._vector_channel(
                        base, query_embedding, min_score_embedding, limit
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"redis vector {filters.object_type} failed: {exc}"
                )
        if not channels:
            return []
        return rrf_merge(channels)[:limit]

    def upsert(self, entries: Sequence[IndexEntry], *, session=None) -> int:
        entries = list(entries)
        if not entries:
            return 0
        settings = get_settings()
        missing = [e for e in entries if e.embedding is None]
        if missing and embedding_available():
            for entry, vec in zip(
                missing, embed_texts([e.searchable_text for e in missing])
            ):
                entry.embedding = vec
        self._ensure_index()
        client = self._redis()
        pipe = client.pipeline(transaction=False)
        for e in entries:
            mapping = {
                "graph_no": settings.graph_no,
                "graph_version": str(settings.graph_version),
                "object_type": e.object_type,
                "object_no": e.object_no,
                "workspace_id": "",
                "searchable_text": e.searchable_text or "",
            }
            if e.embedding is not None:
                mapping["embedding"] = _vec_bytes(e.embedding)
            pipe.hset(
                self._doc_key(
                    settings.graph_no,
                    settings.graph_version,
                    e.object_type,
                    e.object_no,
                ),
                mapping=mapping,
            )
        pipe.execute()
        return len(entries)

    def delete_objects(
        self, object_type: str, object_nos: Sequence[str], *, session=None
    ) -> int:
        object_nos = list(object_nos)
        if not object_nos:
            return 0
        settings = get_settings()
        client = self._redis()
        pipe = client.pipeline(transaction=False)
        for no in object_nos:
            pipe.delete(
                self._doc_key(
                    settings.graph_no, settings.graph_version, object_type, no
                )
            )
        results = pipe.execute()
        return sum(int(bool(r)) for r in results)

    def delete_by_filter(self, filters: SearchFilters, *, session=None) -> int:
        from redis.commands.search.query import Query

        self._ensure_index()
        base = self._base_filter(filters)
        if base == "":
            return 0
        client = self._redis()
        deleted = 0
        # Page through matches deleting each hit by its real key (doc.id). The FT
        # query is graph-scoped via TAG filters, so this is version-safe; one doc
        # maps to many chunk rows, so there is no single id to delete by.
        while True:
            q = Query(base).no_content().paging(0, 200)
            res = client.ft(self._index_name()).search(q)
            if not res.docs:
                break
            pipe = client.pipeline(transaction=False)
            for doc in res.docs:
                pipe.delete(doc.id)
            deleted += sum(int(bool(r)) for r in pipe.execute())
            if len(res.docs) < 200:
                break
        return deleted

    def copy_version(
        self,
        graph_no: str,
        object_type: str,
        object_nos: Sequence[str],
        from_version: int,
        to_version: int,
        *,
        session=None,
    ) -> int:
        object_nos = list(object_nos)
        if not object_nos:
            return 0
        client = self._redis()
        copied = 0
        for no in object_nos:
            src = self._doc_key(graph_no, from_version, object_type, no)
            mapping = client.hgetall(src)
            if not mapping:
                continue
            # Rewrite the version field (bytes hash), keep embedding bytes as-is.
            mapping[b"graph_version"] = str(to_version).encode("utf-8")
            dst = self._doc_key(graph_no, to_version, object_type, no)
            client.hset(dst, mapping=mapping)
            copied += 1
        return copied


def _decode(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
