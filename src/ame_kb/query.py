"""Basic V1 queries: find entities by name, and list a node's direct relations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import or_, select

from .config import get_settings
from .db import session_scope
from .models import GraphEdge, GraphNode
from .store import load_domain_map


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


@dataclass
class GraphNodeDTO:
    graph_node_no: str
    name: str
    type: str


@dataclass
class GraphEdgeDTO:
    graph_edge_no: str
    source_node_no: str
    target_node_no: str
    label: str


@dataclass
class GraphSnapshot:
    nodes: List[GraphNodeDTO]
    edges: List[GraphEdgeDTO]


def graph_snapshot(limit: int = 2000) -> GraphSnapshot:
    """Return the full node + edge set of the active graph version for
    whole-graph visualization. Nodes are capped by ``limit``; edges are kept
    only when both endpoints are within the returned node set.
    """
    settings = get_settings()
    with session_scope() as session:
        node_rows = (
            session.execute(
                select(GraphNode)
                .where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.deleted == 0,
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )
        node_nos = [r.graph_node_no for r in node_rows]
        domain_map = load_domain_map(
            session, settings.graph_no, settings.graph_version, node_nos
        )
        nodes = [
            GraphNodeDTO(
                r.graph_node_no,
                r.name,
                domain_map.get(r.graph_node_no, (r.type, None))[0],
            )
            for r in node_rows
        ]
        node_set = set(node_nos)
        edge_rows = (
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
        edges = [
            GraphEdgeDTO(
                e.graph_edge_no, e.source_node_no, e.target_node_no, e.name
            )
            for e in edge_rows
            if e.source_node_no in node_set and e.target_node_no in node_set
        ]
        return GraphSnapshot(nodes=nodes, edges=edges)


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
        domain_map = load_domain_map(
            session,
            settings.graph_no,
            settings.graph_version,
            [r.graph_node_no for r in rows],
        )
        return [
            NodeHit(
                r.graph_node_no,
                r.name,
                domain_map.get(r.graph_node_no, (r.type, None))[0],
                r.properties or {},
            )
            for r in rows
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
        other_nos = [
            e.target_node_no if e.source_node_no == node_no else e.source_node_no
            for e in edges
        ]
        domain_map = load_domain_map(
            session,
            settings.graph_no,
            settings.graph_version,
            other_nos,
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
            other_type = "?"
            if other is not None:
                other_type = domain_map.get(other_no, (other.type, None))[0]
            hits.append(
                RelationHit(
                    direction=direction,
                    label=e.name,
                    other_no=other_no,
                    other_name=other.name if other else "(missing)",
                    other_type=other_type,
                )
            )
    return hits
