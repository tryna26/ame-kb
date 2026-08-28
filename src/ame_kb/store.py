"""Transactional V3 persistence built from per-document contributions."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import acquire_graph_write_lock, session_scope
from .extract import ExtractionResult
from .ingest import Document
from .migrations import (
    effective_source_id,
    normalize_alias,
    stable_doc_key,
    stable_edge_key,
    stable_mention_key,
)
from .models import (
    DocVersion,
    EdgeContribution,
    GraphEdge,
    GraphNode,
    NodeAlias,
    NodeContribution,
)

_CONFIDENCE_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}


@dataclass
class StoreStats:
    nodes_new: int = 0
    nodes_updated: int = 0
    edges_new: int = 0
    edges_updated: int = 0


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope(model: object, settings: object) -> Tuple[object, object]:
    return (
        model.graph_no == settings.graph_no,
        model.graph_version == settings.graph_version,
    )


def _settings(value: Optional[object]) -> object:
    return value if value is not None else get_settings()


def _doc_identity(
    doc: Document, source_id: Optional[str], settings: object
) -> Tuple[str, str]:
    configured = getattr(settings, "source_id", None)
    effective = effective_source_id(
        source_id if source_id is not None else configured
    )
    return effective, stable_doc_key(effective, doc.doc_id)


def is_unchanged(doc: Document, source_id: Optional[str] = None) -> bool:
    """Return whether this source-qualified document has the stored hash."""

    settings = get_settings()
    _source, doc_key = _doc_identity(doc, source_id, settings)
    with session_scope() as session:
        existing = session.execute(
            select(DocVersion.content_hash).where(
                *_scope(DocVersion, settings), DocVersion.doc_key == doc_key
            )
        ).scalar_one_or_none()
        return existing is not None and existing == content_hash(doc.text)


def _upsert_doc_version(
    session: Session,
    doc: Document,
    source_id: str,
    doc_key: str,
    digest: str,
    settings: object,
) -> None:
    row = session.execute(
        select(DocVersion)
        .where(*_scope(DocVersion, settings), DocVersion.doc_key == doc_key)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        session.add(
            DocVersion(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                source_id=source_id,
                doc_id=doc.doc_id,
                doc_key=doc_key,
                content_hash=digest,
            )
        )
    else:
        row.source_id = source_id
        row.doc_id = doc.doc_id
        row.doc_key = doc_key
        row.content_hash = digest


def mark_processed(doc: Document, source_id: Optional[str] = None) -> None:
    """Compatibility helper guarded against stale hash-only updates.

    V3 callers must use :func:`store_document`.  Updating a hash independently
    is safe only for an initial/no-op mark or when every persisted contribution
    for the document was produced from exactly this content hash.
    """

    settings = get_settings()
    effective, doc_key = _doc_identity(doc, source_id, settings)
    digest = content_hash(doc.text)
    with session_scope() as session:
        acquire_graph_write_lock(
            session, settings.graph_no, settings.graph_version
        )
        existing = session.execute(
            select(DocVersion.content_hash).where(
                *_scope(DocVersion, settings), DocVersion.doc_key == doc_key
            )
        ).scalar_one_or_none()
        node_hashes = list(
            session.execute(
                select(NodeContribution.extraction_hash).where(
                    *_scope(NodeContribution, settings),
                    NodeContribution.doc_key == doc_key,
                )
            ).scalars()
        )
        edge_hashes = list(
            session.execute(
                select(EdgeContribution.extraction_hash).where(
                    *_scope(EdgeContribution, settings),
                    EdgeContribution.doc_key == doc_key,
                )
            ).scalars()
        )
        all_contribution_hashes = [*node_hashes, *edge_hashes]
        if any(value is None for value in all_contribution_hashes):
            raise RuntimeError(
                "refusing to mark content with unattributed legacy "
                "contributions; use store_document()"
            )
        contribution_hashes = set(all_contribution_hashes)
        if contribution_hashes and contribution_hashes != {digest}:
            raise RuntimeError(
                "refusing to mark content whose stored contributions have a "
                "different hash; use store_document()"
            )
        if existing is not None and existing != digest and not contribution_hashes:
            raise RuntimeError(
                "refusing a hash-only update of an existing document; "
                "use store_document()"
            )
        _upsert_doc_version(
            session, doc, effective, doc_key, digest, settings
        )


# V1 helpers remain import-compatible.  They are not used for V3 Node identity.
def slug(name: str) -> str:
    norm = unicodedata.normalize("NFKC", name).strip().lower()
    norm = re.sub(r"\s+", "-", norm)
    norm = re.sub(r"[^0-9a-z\u4e00-\u9fff\-]", "", norm)
    return norm or "unnamed"


def node_no(type_: str, name: str) -> str:
    return f"{type_}:{slug(name)}"


def edge_no(source_no: str, label: str, target_no: str) -> str:
    key = f"{source_no}|{label}|{target_no}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _new_node_no() -> str:
    return f"node:{uuid.uuid4()}"


def _dict(value: object) -> Dict:
    return dict(value) if isinstance(value, dict) else {}


def _is_gap(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_first(first: object, later: object) -> object:
    """Recursively fill gaps while preserving the first stable contribution."""

    if not isinstance(first, dict) or not isinstance(later, dict):
        return later if _is_gap(first) and not _is_gap(later) else first
    merged = dict(first)
    for key, value in later.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_first(merged[key], value)
        elif _is_gap(merged[key]) and not _is_gap(value):
            merged[key] = value
    return merged


def _stable_union(left: object, right: object) -> object:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = (
                _stable_union(merged[key], value) if key in merged else value
            )
        return merged
    if isinstance(left, list) and isinstance(right, list):
        result: List[object] = []
        for value in [*left, *right]:
            if value not in result:
                result.append(value)
        try:
            return sorted(result, key=str)
        except TypeError:
            return result
    return right if _is_gap(left) else left


def _more_complete(values: Iterable[object]) -> str:
    best = ""
    for value in values:
        candidate = str(value or "").strip()
        if len(candidate) > len(best):
            best = candidate
    return best


def _dedupe_strings(values: Iterable[object]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _canonical_node_no(
    session: Session, node_no_value: str, settings: object, *, lock: bool = False
) -> str:
    """Follow merge redirects and reject broken or cyclic chains."""

    current = node_no_value
    visited: Set[str] = set()
    while True:
        if current in visited:
            raise ValueError(f"merged_into cycle detected at {current!r}")
        visited.add(current)
        stmt = select(GraphNode).where(
            *_scope(GraphNode, settings), GraphNode.graph_node_no == current
        )
        if lock:
            stmt = stmt.with_for_update()
        node = session.execute(stmt).scalar_one_or_none()
        if node is None:
            raise ValueError(f"contribution points to missing node {current!r}")
        parent = str(node.merged_into or "").strip()
        if parent:
            current = parent
            continue
        if int(node.deleted or 0) != 0:
            raise ValueError(f"node {current!r} is deleted without a merge redirect")
        return current


def _replace_aliases(
    session: Session, node: GraphNode, aliases: Sequence[str], settings: object
) -> None:
    # Document aggregation owns only ingest-derived aliases.  Migration, merge,
    # and future manual aliases are durable compatibility data and must survive
    # a document replacement.
    session.execute(
        delete(NodeAlias).where(
            *_scope(NodeAlias, settings),
            NodeAlias.canonical_node_no == node.graph_node_no,
            NodeAlias.source == "ingest",
        )
    )
    durable_normalized = set(
        session.execute(
            select(NodeAlias.normalized_alias).where(
                *_scope(NodeAlias, settings),
                NodeAlias.canonical_node_no == node.graph_node_no,
                NodeAlias.source != "ingest",
            )
        ).scalars()
    )
    by_normalized: Dict[str, str] = {}
    for alias in aliases:
        normalized = normalize_alias(alias)
        if not normalized:
            continue
        current = by_normalized.get(normalized, "")
        if len(alias) > len(current):
            by_normalized[normalized] = alias
    for normalized in sorted(by_normalized):
        if normalized in durable_normalized:
            continue
        session.add(
            NodeAlias(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                type=node.type,
                normalized_alias=normalized,
                canonical_node_no=node.graph_node_no,
                alias=by_normalized[normalized],
                source="ingest",
            )
        )


def recompute_nodes(
    session: Session, node_nos: Iterable[str], settings: Optional[object] = None
) -> StoreStats:
    """Rebuild selected canonical Nodes solely from all their contributions."""

    settings = _settings(settings)
    stats = StoreStats()
    for node_no_value in sorted(set(node_nos)):
        node = session.execute(
            select(GraphNode)
            .where(
                *_scope(GraphNode, settings),
                GraphNode.graph_node_no == node_no_value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        contributions = list(
            session.execute(
                select(NodeContribution)
                .where(
                    *_scope(NodeContribution, settings),
                    NodeContribution.canonical_node_no == node_no_value,
                )
                .order_by(
                    NodeContribution.canonical_rank,
                    NodeContribution.doc_key,
                    NodeContribution.mention_key,
                    NodeContribution.id,
                )
            )
            .scalars()
            .all()
        )
        if not contributions:
            if node is not None and not node.merged_into:
                node.deleted = 1
                node.description = None
                node.aliases = []
                node.properties = {}
                node.ref = {}
                node.embedding = None
                node.embedding_hash = None
                node.embedding_model = None
                _replace_aliases(session, node, [], settings)
                stats.nodes_updated += 1
            continue

        types = {row.type for row in contributions}
        if len(types) != 1:
            raise ValueError(
                f"canonical node {node_no_value!r} has mixed contribution types"
            )
        properties: object = {}
        ref: object = {}
        names: List[str] = []
        for row in contributions:
            properties = _merge_first(properties, _dict(row.properties))
            ref = _stable_union(ref, _dict(row.ref))
            names.append(row.name)
        name = _more_complete(names)
        durable_aliases = list(
            session.execute(
                select(NodeAlias.alias).where(
                    *_scope(NodeAlias, settings),
                    NodeAlias.canonical_node_no == node_no_value,
                    NodeAlias.source != "ingest",
                )
            ).scalars()
        )
        ingest_aliases = _dedupe_strings([name, *names])
        aliases = _dedupe_strings([*ingest_aliases, *durable_aliases])
        if node is None:
            node = GraphNode(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                graph_node_no=node_no_value,
            )
            session.add(node)
            stats.nodes_new += 1
        else:
            if node.merged_into:
                raise ValueError(
                    f"cannot aggregate contributions into merged loser {node_no_value!r}"
                )
            stats.nodes_updated += 1
        node.name = name
        node.type = contributions[0].type
        node.description = None
        node.properties = _dict(properties)
        node.ref = _dict(ref)
        node.aliases = aliases
        node.deleted = 0
        node.embedding = None
        node.embedding_hash = None
        node.embedding_model = None
        _replace_aliases(session, node, ingest_aliases, settings)
    return stats


def recompute_edges(
    session: Session,
    edge_nos: Optional[Iterable[str]] = None,
    settings: Optional[object] = None,
) -> StoreStats:
    """Rebuild selected (or all) canonical Edges solely from contributions."""

    settings = _settings(settings)
    requested = None if edge_nos is None else set(edge_nos)
    if requested is not None and not requested:
        return StoreStats()
    contribution_stmt = select(EdgeContribution).where(
        *_scope(EdgeContribution, settings)
    )
    contributions = list(
        session.execute(
            contribution_stmt.order_by(
                EdgeContribution.source_node_no,
                EdgeContribution.name,
                EdgeContribution.target_node_no,
                EdgeContribution.doc_key,
                EdgeContribution.edge_key,
                EdgeContribution.id,
            )
        )
        .scalars()
        .all()
    )
    original_nos: Set[str] = set()
    original_no_by_id: Dict[int, str] = {}
    for row in contributions:
        original_no = edge_no(row.source_node_no, row.name, row.target_node_no)
        original_nos.add(original_no)
        original_no_by_id[id(row)] = original_no
        source = _canonical_node_no(
            session, row.source_node_no, settings, lock=True
        )
        target = _canonical_node_no(
            session, row.target_node_no, settings, lock=True
        )
        if source != row.source_node_no:
            row.source_node_no = source
        if target != row.target_node_no:
            row.target_node_no = target
    grouped: Dict[Tuple[str, str, str], List[EdgeContribution]] = defaultdict(list)
    for row in contributions:
        key = (row.source_node_no, row.name, row.target_node_no)
        aggregate_no = edge_no(key[0], key[1], key[2])
        if (
            row.source_node_no != row.target_node_no
            and (
                requested is None
                or aggregate_no in requested
                or original_no_by_id[id(row)] in requested
            )
        ):
            grouped[key].append(row)

    aggregate_nos = {
        edge_no(source, label, target) for source, label, target in grouped
    }
    if requested is None:
        existing_stmt = select(GraphEdge).where(*_scope(GraphEdge, settings))
    else:
        lookup_nos = requested | aggregate_nos
        existing_stmt = select(GraphEdge).where(
            *_scope(GraphEdge, settings), GraphEdge.graph_edge_no.in_(lookup_nos)
        )
    existing = {
        row.graph_edge_no: row
        for row in session.execute(existing_stmt.with_for_update()).scalars().all()
    }
    stats = StoreStats()
    target_nos = set(existing) | aggregate_nos
    if requested is None:
        target_nos |= original_nos
    rows_by_no = {
        edge_no(source, label, target): rows
        for (source, label, target), rows in grouped.items()
    }
    for aggregate_no in sorted(target_nos):
        rows = rows_by_no.get(aggregate_no, [])
        edge = existing.get(aggregate_no)
        if not rows:
            if edge is not None:
                edge.deleted = 1
                edge.description = None
                edge.properties = {}
                edge.ref = {}
                stats.edges_updated += 1
            continue
        source = rows[0].source_node_no
        label = rows[0].name
        target = rows[0].target_node_no
        properties: object = {}
        ref: object = {}
        confidence = "AMBIGUOUS"
        for row in rows:
            properties = _merge_first(properties, _dict(row.properties))
            ref = _stable_union(ref, _dict(row.ref))
            candidate = str(row.confidence or "INFERRED").upper()
            if candidate not in _CONFIDENCE_RANK:
                raise ValueError(f"invalid edge confidence {candidate!r}")
            if _CONFIDENCE_RANK[candidate] > _CONFIDENCE_RANK[confidence]:
                confidence = candidate
        properties = _dict(properties)
        properties["confidence"] = confidence
        if edge is None:
            edge = GraphEdge(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                graph_edge_no=aggregate_no,
            )
            session.add(edge)
            stats.edges_new += 1
        else:
            stats.edges_updated += 1
        edge.source_node_no = source
        edge.target_node_no = target
        edge.name = label
        edge.description = None
        edge.properties = properties
        edge.ref = _dict(ref)
        edge.deleted = 0
    return stats


def _resolve_old_assignments(
    session: Session, rows: Sequence[NodeContribution], settings: object
) -> Dict[str, Tuple[str, int]]:
    assignments: Dict[str, Tuple[str, int]] = {}
    for row in rows:
        assignments[row.mention_key] = (
            _canonical_node_no(
                session, row.canonical_node_no, settings, lock=True
            ),
            int(row.canonical_rank or 0),
        )
    return assignments


def _assign_document_nodes(
    session: Session,
    old_rows: Sequence[NodeContribution],
    mentions_by_id: Dict[str, object],
    mention_keys: Dict[str, str],
    settings: object,
) -> Dict[str, Tuple[str, int]]:
    """Map new mentions to durable Nodes before replacing old rows.

    Exact business mention keys win.  Then a one-to-one unmatched mention of
    the same type is treated as an in-place rename.  LLM-local ``mention_id``
    is persisted for audit/edge addressing but is deliberately not trusted as
    a cross-run identity: models may renumber m1/m2 after a document edit.
    Multiple unmatched entities of one type are left unresolved rather than
    guessing and swapping durable identities.
    """

    old_values = []
    for row in old_rows:
        old_values.append(
            {
                "row": row,
                "node_no": _canonical_node_no(
                    session, row.canonical_node_no, settings, lock=True
                ),
                "rank": int(row.canonical_rank or 0),
            }
        )
    assignments: Dict[str, Tuple[str, int]] = {}
    used_ids: Set[int] = set()

    def assign(mention_id: str, item: Dict[str, object]) -> None:
        row = item["row"]
        assignments[mention_id] = (str(item["node_no"]), int(item["rank"]))
        used_ids.add(int(row.id))

    # 1. Exact type + normalized name identity.
    for mention_id, node in mentions_by_id.items():
        key = mention_keys[mention_id]
        matches = [
            item
            for item in old_values
            if item["row"].mention_key == key and int(item["row"].id) not in used_ids
        ]
        if len(matches) == 1:
            assign(mention_id, matches[0])

    # 2. Safe one-to-one rename fallback within a type.
    types = {node.type for node in mentions_by_id.values()}
    for type_ in types:
        new_ids = [
            mention_id
            for mention_id, node in mentions_by_id.items()
            if mention_id not in assignments and node.type == type_
        ]
        old_items = [
            item
            for item in old_values
            if int(item["row"].id) not in used_ids and item["row"].type == type_
        ]
        if len(new_ids) == len(old_items) == 1:
            assign(new_ids[0], old_items[0])

    for mention_id in mentions_by_id:
        assignments.setdefault(mention_id, (_new_node_no(), 0))
    return assignments


def _preserve_renamed_aliases(
    session: Session,
    old_rows: Sequence[NodeContribution],
    mentions_by_id: Dict[str, object],
    assignments: Dict[str, Tuple[str, int]],
    settings: object,
) -> None:
    """Promote previous names to durable history aliases on a rename."""

    names_by_node: Dict[str, Set[str]] = defaultdict(set)
    for mention_id, node in mentions_by_id.items():
        node_no_value, _rank = assignments[mention_id]
        names_by_node[node_no_value].add(normalize_alias(node.name))

    for row in old_rows:
        node_no_value = _canonical_node_no(
            session, row.canonical_node_no, settings, lock=True
        )
        normalized = normalize_alias(row.name)
        if not normalized or normalized in names_by_node.get(node_no_value, set()):
            continue
        existing = session.execute(
            select(NodeAlias).where(
                *_scope(NodeAlias, settings),
                NodeAlias.type == row.type,
                NodeAlias.normalized_alias == normalized,
                NodeAlias.canonical_node_no == node_no_value,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.source == "ingest":
                existing.source = "history"
            continue
        session.add(
            NodeAlias(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                type=row.type,
                normalized_alias=normalized,
                canonical_node_no=node_no_value,
                alias=row.name,
                source="history",
            )
        )


def _validated_mentions(
    result: ExtractionResult,
) -> Tuple[Dict[str, object], Dict[str, str]]:
    by_id: Dict[str, object] = {}
    mention_keys: Dict[str, str] = {}
    seen: Set[str] = set()
    for node in result.nodes:
        key = stable_mention_key(node.type, node.name)
        mention_id = str(node.mention_id or "").strip() or f"legacy:{key}"
        if mention_id in by_id:
            raise ValueError(f"duplicate mention_id {mention_id!r}")
        if key in seen:
            raise ValueError(f"duplicate stable mention {node.type}:{node.name}")
        seen.add(key)
        by_id[mention_id] = node
        mention_keys[mention_id] = key
    return by_id, mention_keys


def _edge_endpoint_id(
    edge: object,
    attribute: str,
    legacy_attribute: str,
    mentions_by_id: Dict[str, object],
) -> str:
    mention_id = str(getattr(edge, attribute, None) or "").strip()
    if mention_id:
        return mention_id
    legacy_name = str(getattr(edge, legacy_attribute, None) or "").strip()
    matches = [
        key
        for key, node in mentions_by_id.items()
        if normalize_alias(str(getattr(node, "name", "") or ""))
        == normalize_alias(legacy_name)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"edge endpoint {legacy_name!r} does not identify one document mention"
        )
    return matches[0]


def store_document(
    result: ExtractionResult, doc: Document, source_id: Optional[str] = None
) -> StoreStats:
    """Atomically replace one document's contributions and materialized graph."""

    if result.doc_id != doc.doc_id:
        raise ValueError(
            f"extraction/doc mismatch: {result.doc_id!r} != {doc.doc_id!r}"
        )
    settings = get_settings()
    effective, doc_key = _doc_identity(doc, source_id, settings)
    digest = content_hash(doc.text)
    mentions_by_id, mention_keys = _validated_mentions(result)

    with session_scope() as session:
        acquire_graph_write_lock(
            session, settings.graph_no, settings.graph_version
        )
        session.execute(
            select(DocVersion.id)
            .where(*_scope(DocVersion, settings), DocVersion.doc_key == doc_key)
            .with_for_update()
        ).scalar_one_or_none()
        old_nodes = list(
            session.execute(
                select(NodeContribution)
                .where(
                    *_scope(NodeContribution, settings),
                    NodeContribution.doc_key == doc_key,
                )
                .order_by(NodeContribution.mention_key, NodeContribution.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        old_edges = list(
            session.execute(
                select(EdgeContribution)
                .where(
                    *_scope(EdgeContribution, settings),
                    EdgeContribution.doc_key == doc_key,
                )
                .order_by(EdgeContribution.edge_key, EdgeContribution.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        old_assignments = _resolve_old_assignments(session, old_nodes, settings)
        new_assignments = _assign_document_nodes(
            session, old_nodes, mentions_by_id, mention_keys, settings
        )
        _preserve_renamed_aliases(
            session, old_nodes, mentions_by_id, new_assignments, settings
        )
        old_node_nos = {node_no_value for node_no_value, _rank in old_assignments.values()}
        old_edge_nos = {
            edge_no(row.source_node_no, row.name, row.target_node_no)
            for row in old_edges
        }
        old_edge_triples = {
            (
                _canonical_node_no(session, row.source_node_no, settings, lock=True),
                row.name,
                _canonical_node_no(session, row.target_node_no, settings, lock=True),
            )
            for row in old_edges
        }

        session.execute(
            delete(EdgeContribution).where(
                *_scope(EdgeContribution, settings),
                EdgeContribution.doc_key == doc_key,
            )
        )
        session.execute(
            delete(NodeContribution).where(
                *_scope(NodeContribution, settings),
                NodeContribution.doc_key == doc_key,
            )
        )

        node_for_mention: Dict[str, str] = {}
        for mention_id, node in mentions_by_id.items():
            mention_key = mention_keys[mention_id]
            canonical_no, canonical_rank = new_assignments[mention_id]
            node_for_mention[mention_id] = canonical_no
            session.add(
                NodeContribution(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    source_id=effective,
                    doc_id=doc.doc_id,
                    doc_key=doc_key,
                    mention_key=mention_key,
                    mention_id=mention_id,
                    canonical_node_no=canonical_no,
                    canonical_rank=canonical_rank,
                    name=node.name,
                    type=node.type,
                    properties=_dict(node.properties),
                    ref={f"{effective}::{doc.doc_id}": _dedupe_strings(node.source)},
                    extraction_hash=digest,
                )
            )

        new_edge_triples: Set[Tuple[str, str, str]] = set()
        edge_keys_seen: Set[str] = set()
        for edge in result.edges:
            source_id_value = _edge_endpoint_id(
                edge, "source_mention_id", "source_name", mentions_by_id
            )
            target_id_value = _edge_endpoint_id(
                edge, "target_mention_id", "target_name", mentions_by_id
            )
            if (
                source_id_value not in mentions_by_id
                or target_id_value not in mentions_by_id
            ):
                raise ValueError(
                    f"edge {edge.label!r} references an unknown mention endpoint"
                )
            source_mention_key = mention_keys[source_id_value]
            target_mention_key = mention_keys[target_id_value]
            contribution_key = stable_edge_key(
                source_mention_key, edge.label, target_mention_key
            )
            if contribution_key in edge_keys_seen:
                raise ValueError(f"duplicate stable edge {edge.label!r}")
            edge_keys_seen.add(contribution_key)
            source_no = node_for_mention[source_id_value]
            target_no = node_for_mention[target_id_value]
            confidence = str(edge.confidence or "INFERRED").upper()
            if confidence not in _CONFIDENCE_RANK:
                raise ValueError(f"invalid edge confidence {confidence!r}")
            new_edge_triples.add((source_no, edge.label, target_no))
            session.add(
                EdgeContribution(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    source_id=effective,
                    doc_id=doc.doc_id,
                    doc_key=doc_key,
                    edge_key=contribution_key,
                    source_mention_key=source_mention_key,
                    target_mention_key=target_mention_key,
                    source_node_no=source_no,
                    target_node_no=target_no,
                    name=edge.label,
                    confidence=confidence,
                    properties={"confidence": confidence},
                    ref={f"{effective}::{doc.doc_id}": _dedupe_strings(edge.source)},
                    extraction_hash=digest,
                )
            )

        session.flush()
        node_stats = recompute_nodes(
            session, old_node_nos | set(node_for_mention.values()), settings
        )
        affected_edge_nos = {
            edge_no(source, label, target)
            for source, label, target in old_edge_triples | new_edge_triples
        } | old_edge_nos
        edge_stats = recompute_edges(session, affected_edge_nos, settings)
        _upsert_doc_version(session, doc, effective, doc_key, digest, settings)
        return StoreStats(
            nodes_new=node_stats.nodes_new,
            nodes_updated=node_stats.nodes_updated,
            edges_new=edge_stats.edges_new,
            edges_updated=edge_stats.edges_updated,
        )


def store(result: ExtractionResult) -> StoreStats:
    """Deprecated V2 wrapper; use :func:`store_document` with real text."""

    warnings.warn(
        "store(result) is deprecated; use store_document(result, doc, source_id)",
        DeprecationWarning,
        stacklevel=2,
    )
    pseudo_doc = Document(doc_id=result.doc_id, path=Path(result.doc_id), text="")
    return store_document(result, pseudo_doc)
