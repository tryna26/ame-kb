"""Rebuild the search index from the current nodes/edges/chunks.

Used by `ame-kb reindex` after enabling or rotating the embedding endpoint, or
after switching SEARCH_BACKEND, so existing rows get (re)embedded/reindexed into
the active backend without re-running extraction.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import DocChunk, GraphEdge, GraphNode
from .searchindex import (
    DOC_CHUNK,
    EDGE,
    NODE,
    aliases_for,
    build_searchable_text,
    upsert_search_index,
)


def reindex_all() -> Tuple[int, int, int]:
    settings = get_settings()
    with session_scope() as session:
        nodes = (
            session.execute(
                select(GraphNode).where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.deleted == 0,
                )
            )
            .scalars()
            .all()
        )
        edges = (
            session.execute(
                select(GraphEdge).where(
                    GraphEdge.graph_no == settings.graph_no,
                    GraphEdge.graph_version == settings.graph_version,
                    GraphEdge.deleted == 0,
                )
            )
            .scalars()
            .all()
        )
        chunks = (
            session.execute(
                select(DocChunk).where(
                    DocChunk.graph_no == settings.graph_no,
                    DocChunk.graph_version == settings.graph_version,
                )
            )
            .scalars()
            .all()
        )

        alias_map = aliases_for(session, [n.graph_node_no for n in nodes])
        entries: List[Dict] = []
        for n in nodes:
            entries.append(
                {
                    "object_type": NODE,
                    "object_no": n.graph_node_no,
                    "searchable_text": build_searchable_text(
                        n.name,
                        n.description,
                        n.properties or {},
                        alias_map.get(n.graph_node_no),
                    ),
                }
            )
        for e in edges:
            entries.append(
                {
                    "object_type": EDGE,
                    "object_no": e.graph_edge_no,
                    "searchable_text": build_searchable_text(
                        e.name, e.description, e.properties or {}
                    ),
                }
            )
        for c in chunks:
            entries.append(
                {
                    "object_type": DOC_CHUNK,
                    "object_no": c.chunk_no,
                    "searchable_text": c.content or "",
                }
            )
        upsert_search_index(session, entries)

    return len(nodes), len(edges), len(chunks)
