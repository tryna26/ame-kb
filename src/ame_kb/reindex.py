"""Rebuild kg_search_index from the current nodes/edges.

Used by `ame-kb reindex` after enabling or rotating the embedding endpoint, so
existing rows get (re)embedded without re-running extraction.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import GraphEdge, GraphNode
from .searchindex import EDGE, NODE, build_searchable_text, upsert_search_index


def reindex_all() -> Tuple[int, int]:
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

        entries: List[Dict] = []
        for n in nodes:
            entries.append(
                {
                    "object_type": NODE,
                    "object_no": n.graph_node_no,
                    "searchable_text": build_searchable_text(
                        n.name, n.description, n.properties or {}
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
        upsert_search_index(session, entries)

    return len(nodes), len(edges)
