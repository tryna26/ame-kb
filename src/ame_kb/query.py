"""Canonical V3 entity lookup and one-hop relation queries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .models import GraphEdge, GraphNode, LegacyNodeId, NodeAlias
from .resolve import normalize_alias

MAX_REDIRECT_HOPS = 32


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


def _scope(model: object) -> Tuple[object, object]:
    settings = get_settings()
    return (
        model.graph_no == settings.graph_no,
        model.graph_version == settings.graph_version,
    )


def _raw_node_by_no(session: Session, node_no: str) -> Optional[GraphNode]:
    return session.execute(
        select(GraphNode).where(
            *_scope(GraphNode),
            GraphNode.graph_node_no == node_no,
        )
    ).scalar_one_or_none()


def resolve_node_identifier(
    session: Session, identifier: str, max_hops: int = MAX_REDIRECT_HOPS
) -> Optional[GraphNode]:
    """Resolve a current or legacy Node ID to its live canonical survivor.

    Current IDs take precedence over the legacy compatibility map. Unknown IDs
    and terminal soft-deleted rows return ``None``. A redirect cycle or a chain
    longer than ``max_hops`` is treated as corrupt data and raises ``ValueError``.
    """

    if max_hops < 0:
        raise ValueError("max_hops must not be negative")
    current_no = str(identifier or "").strip()
    if not current_no:
        return None

    node = _raw_node_by_no(session, current_no)
    if node is None:
        mapped = session.execute(
            select(LegacyNodeId.canonical_node_no).where(
                *_scope(LegacyNodeId),
                LegacyNodeId.legacy_node_no == current_no,
            )
        ).scalar_one_or_none()
        if mapped is None:
            return None
        current_no = str(mapped)

    visited = set()
    redirects = 0
    while True:
        if current_no in visited:
            raise ValueError(
                f"merged_into cycle detected while resolving {identifier!r}"
            )
        visited.add(current_no)
        node = _raw_node_by_no(session, current_no)
        if node is None:
            return None
        merged_into = str(node.merged_into or "").strip()
        if not merged_into:
            return node if int(node.deleted or 0) == 0 else None
        redirects += 1
        if redirects > max_hops:
            raise ValueError(
                f"merged_into chain exceeds {max_hops} hop(s) for {identifier!r}"
            )
        current_no = merged_into


def _as_hit(node: GraphNode) -> NodeHit:
    return NodeHit(
        graph_node_no=node.graph_node_no,
        name=node.name,
        type=node.type,
        properties=node.properties or {},
    )


def find_entities(
    name: str, limit: int = 20, type_filter: Optional[str] = None
) -> List[NodeHit]:
    """Find live canonical Nodes by name substring or alias substring.

    Exact canonical-name and exact-alias matches sort before substring matches.
    Alias rows are canonicalized defensively, so stale rows that still target a
    merged loser return its survivor. Each survivor appears at most once.
    """

    query = str(name or "").strip()
    if not query or limit <= 0:
        return []
    normalized = normalize_alias(query)

    with session_scope() as session:
        node_stmt = select(GraphNode).where(
            *_scope(GraphNode),
            GraphNode.deleted == 0,
            GraphNode.merged_into.is_(None),
            GraphNode.name.contains(query, autoescape=True),
        )
        if type_filter:
            node_stmt = node_stmt.where(GraphNode.type == type_filter)
        nodes = list(
            session.execute(
                node_stmt.order_by(GraphNode.name, GraphNode.graph_node_no)
            )
            .scalars()
            .all()
        )

        alias_rows = []
        if normalized:
            alias_stmt = select(NodeAlias).where(
                *_scope(NodeAlias),
                NodeAlias.normalized_alias.contains(normalized, autoescape=True),
            )
            if type_filter:
                alias_stmt = alias_stmt.where(NodeAlias.type == type_filter)
            alias_rows = list(
                session.execute(
                    alias_stmt.order_by(NodeAlias.alias, NodeAlias.canonical_node_no)
                )
                .scalars()
                .all()
            )

        ranked: Dict[str, Tuple[int, GraphNode]] = {}

        def add(node: Optional[GraphNode], rank: int) -> None:
            if node is None or node.merged_into or int(node.deleted or 0) != 0:
                return
            if type_filter and node.type != type_filter:
                return
            previous = ranked.get(node.graph_node_no)
            if previous is None or rank < previous[0]:
                ranked[node.graph_node_no] = (rank, node)

        for node in nodes:
            rank = 0 if normalize_alias(node.name) == normalized else 2
            add(node, rank)
        for alias in alias_rows:
            node = resolve_node_identifier(session, alias.canonical_node_no)
            rank = 1 if alias.normalized_alias == normalized else 3
            add(node, rank)

        ordered = sorted(
            ranked.values(),
            key=lambda item: (
                item[0],
                item[1].name.casefold(),
                item[1].graph_node_no,
            ),
        )
        return [_as_hit(node) for _rank, node in ordered[:limit]]


def find_entities_exact(type_: str, name: str) -> List[NodeHit]:
    """Return canonical Nodes whose type and name/alias exactly match.

    This powers the compatibility ``node-no`` CLI. It performs a lookup and
    never derives a synthetic identity from the supplied name.
    """

    type_value = str(type_ or "").strip()
    name_value = str(name or "").strip()
    normalized = normalize_alias(name_value)
    if not type_value or not name_value or not normalized:
        return []

    with session_scope() as session:
        nodes = list(
            session.execute(
                select(GraphNode)
                .where(
                    *_scope(GraphNode),
                    GraphNode.deleted == 0,
                    GraphNode.merged_into.is_(None),
                    GraphNode.type == type_value,
                    GraphNode.name == name_value,
                )
                .order_by(GraphNode.graph_node_no)
            )
            .scalars()
            .all()
        )
        aliases = (
            session.execute(
                select(NodeAlias)
                .where(
                    *_scope(NodeAlias),
                    NodeAlias.type == type_value,
                    NodeAlias.normalized_alias == normalized,
                )
                .order_by(NodeAlias.canonical_node_no)
            )
            .scalars()
            .all()
        )
        by_no: Dict[str, GraphNode] = {node.graph_node_no: node for node in nodes}
        for alias in aliases:
            node = resolve_node_identifier(session, alias.canonical_node_no)
            if node is not None and node.type == type_value:
                by_no[node.graph_node_no] = node
        return [_as_hit(by_no[node_no]) for node_no in sorted(by_no)]


def _node_by_no(session: Session, node_no: str) -> Optional[GraphNode]:
    """Backward-compatible internal helper returning a canonical Node."""

    return resolve_node_identifier(session, node_no)


def relations_of(node_no: str) -> List[RelationHit]:
    """Return direct relations for a current, legacy, or merged Node ID."""

    hits: List[RelationHit] = []
    with session_scope() as session:
        node = resolve_node_identifier(session, node_no)
        if node is None:
            return []
        canonical_no = node.graph_node_no
        edges = (
            session.execute(
                select(GraphEdge)
                .where(
                    *_scope(GraphEdge),
                    GraphEdge.deleted == 0,
                    or_(
                        GraphEdge.source_node_no == canonical_no,
                        GraphEdge.target_node_no == canonical_no,
                    ),
                )
                .order_by(GraphEdge.id, GraphEdge.graph_edge_no)
            )
            .scalars()
            .all()
        )
        seen = set()
        for edge in edges:
            if edge.source_node_no == canonical_no:
                direction = "out"
                other_identifier = edge.target_node_no
            else:
                direction = "in"
                other_identifier = edge.source_node_no
            other = resolve_node_identifier(session, other_identifier)
            if other is None or other.graph_node_no == canonical_no:
                continue
            key = (direction, edge.name, other.graph_node_no)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                RelationHit(
                    direction=direction,
                    label=edge.name,
                    other_no=other.graph_node_no,
                    other_name=other.name,
                    other_type=other.type,
                )
            )
    return hits
