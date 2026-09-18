"""Search backend abstraction (HybridIndex port).

recall.py and the indexing path talk only to this interface, never to a concrete
store. Full-text + vector retrieval, their RRF fusion, and index writes all live
behind `HybridIndex`, so the backend is swappable via SEARCH_BACKEND=mysql|redis
without touching recall logic. Mirrors general_recall/domain/service/recall/
ports.go (IByteRAGRetriever): every search carries two independent min-score
thresholds (text + embedding) and returns a fused ranked list.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

try:  # Protocol is stdlib on 3.8+, but guard for very old typing shims.
    from typing import Protocol
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol  # type: ignore


@dataclass
class SearchFilters:
    """Scoping for a single search.

    graph_no/graph_version form a paired scope (retrieve_params.go GraphScope).
    restrict_object_nos reranks within a fixed candidate set (the pool / neighbor
    rerank steps). workspace_ids is wired but dormant in V4 (single-tenant).
    ignore_graph_scope drops the graph filter for the doc-chunk fallback channel
    (sources.md ④b: kg_doc_chunk not bound by graph_scope).
    """

    graph_no: str
    graph_version: int
    object_type: str
    restrict_object_nos: Optional[Sequence[str]] = None
    workspace_ids: Optional[Sequence[str]] = None
    ignore_graph_scope: bool = False


@dataclass
class IndexEntry:
    """One row to (re)index. Embedding is precomputed by the caller so embedding
    logic stays backend-independent; a backend that cannot store vectors simply
    ignores it."""

    object_type: str
    object_no: str
    searchable_text: str
    embedding: Optional[List[float]] = None


class HybridIndex(Protocol):
    """Full-text + vector index with RRF fusion behind one call."""

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
        """Return object_nos ranked by fused relevance (best first), truncated to
        `limit`. A failing channel only appends to `warnings` and continues; if
        every channel fails the result is empty."""
        ...

    def upsert(self, entries: Sequence[IndexEntry], *, session=None) -> int:
        """Insert/update index rows. `session` lets a SQL backend join the
        caller's transaction; backends without transactions ignore it."""
        ...

    def delete_objects(
        self, object_type: str, object_nos: Sequence[str], *, session=None
    ) -> int:
        """Delete index rows by business key."""
        ...

    def delete_by_filter(self, filters: SearchFilters, *, session=None) -> int:
        """Delete every index row matching `filters` (used to drop a doc's chunks
        before re-chunking, since one doc maps to many chunk rows)."""
        ...

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
        """Copy index rows for `object_nos` from (graph_no, from_version) to
        (graph_no, to_version), preserving the stored embedding so projected
        objects stay searchable without being re-embedded. Returns rows copied.
        A SQL backend joins the caller's `session`; others ignore it."""
        ...
