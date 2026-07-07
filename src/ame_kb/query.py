"""Basic V1 queries: find entities by name, and list a node's direct relations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import or_, select

from .config import get_settings
from .db import session_scope
from .models import GraphEdge, GraphNode


@dataclass
class NodeHit:
    graph_node_no: str
    name: str
    type: str
    properties: dict


@dataclass
class RelationHit:
    direction: str  # "out" or "in"
    label: str
    other_no: str
    other_name: str
    other_type: str


def find_entities(name: str, limit: int = 20) -> List[NodeHit]:
    settings = get_settings()
    with session_scope() as session:
        rows = (
            session.execute(
                select(GraphNode)
                .where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.deleted == 0,
                    GraphNode.name.like(f"%{name}%"),
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [
            NodeHit(r.graph_node_no, r.name, r.type, r.properties or {}) for r in rows
        ]


def _node_by_no(session, node_no: str) -> Optional[GraphNode]:
    settings = get_settings()
    return session.execute(
        select(GraphNode).where(
            GraphNode.graph_no == settings.graph_no,
            GraphNode.graph_version == settings.graph_version,
            GraphNode.graph_node_no == node_no,
            GraphNode.deleted == 0,
        )
    ).scalar_one_or_none()


def relations_of(node_no: str) -> List[RelationHit]:
    """Return the direct (one-hop) relations of a node identified by graph_node_no."""
    settings = get_settings()
    hits: List[RelationHit] = []
    with session_scope() as session:
        edges = (
            session.execute(
                select(GraphEdge).where(
                    GraphEdge.graph_no == settings.graph_no,
                    GraphEdge.graph_version == settings.graph_version,
                    GraphEdge.deleted == 0,
                    or_(
                        GraphEdge.source_node_no == node_no,
                        GraphEdge.target_node_no == node_no,
                    ),
                )
            )
            .scalars()
            .all()
        )
        for e in edges:
            if e.source_node_no == node_no:
                other = _node_by_no(session, e.target_node_no)
                direction = "out"
                other_no = e.target_node_no
            else:
                other = _node_by_no(session, e.source_node_no)
                direction = "in"
                other_no = e.source_node_no
            hits.append(
                RelationHit(
                    direction=direction,
                    label=e.name,
                    other_no=other_no,
                    other_name=other.name if other else "(missing)",
                    other_type=other.type if other else "?",
                )
            )
    return hits
