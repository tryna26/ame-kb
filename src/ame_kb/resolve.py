"""Offline, transactional cross-document entity resolution.

The resolver deliberately makes one pairwise decision at a time.  It never
turns an ``A == B`` and ``B == C`` result into an unexamined ``A == C``
union-find component: after every successful merge it reloads the survivor and
continues with the graph's current state.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .db import acquire_graph_write_lock, session_scope
from .embed import ensure_embeddings
from .extract import _extract_json, call_llm
from .models import (
    EdgeContribution,
    GraphEdge,
    GraphNode,
    MergeLog,
    NodeAlias,
    NodeContribution,
)
from .migrations import normalize_alias
from .config import get_settings
from .vecmath import cosine

logger = logging.getLogger(__name__)

_DECISIONS = {"same", "related", "different"}
_CONFIDENCE_RANK = {"AMBIGUOUS": 0, "INFERRED": 1, "EXTRACTED": 2}


@dataclass(frozen=True)
class JudgeResult:
    decision: str
    canonical_name: str = ""
    reason: str = ""

    @property
    def same(self) -> bool:
        return self.decision == "same"


@dataclass
class ResolveStats:
    scanned: int = 0
    pairs: int = 0
    exact_merged: int = 0
    high_merged: int = 0
    llm_merged: int = 0
    skipped: int = 0
    failed: int = 0
    judgments: List[str] = field(default_factory=list)

    @property
    def merged(self) -> int:
        return self.exact_merged + self.high_merged + self.llm_merged


def _load_prompt() -> str:
    return (
        resources.files("ame_kb.prompts")
        .joinpath("resolve_v3.txt")
        .read_text(encoding="utf-8")
    )


def _surface_names(node: object) -> List[str]:
    names: List[str] = []
    for value in [getattr(node, "name", ""), *(getattr(node, "aliases", None) or [])]:
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _node_view(node: object) -> Dict[str, object]:
    return {
        "name": str(getattr(node, "name", "") or ""),
        "type": str(getattr(node, "type", "") or ""),
        "description": str(getattr(node, "description", "") or ""),
        "properties": getattr(node, "properties", None) or {},
        "aliases": _surface_names(node),
    }


def _valid_canonical_name(value: str, *nodes: object) -> bool:
    value = str(value or "").strip()
    return bool(value) and any(value in _surface_names(node) for node in nodes)


def _decision_fingerprint(node: object) -> str:
    """Hash exactly the node fields used for pair adjudication."""

    payload = json.dumps(
        _node_view(node),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _persistent_decision_fingerprint(
    session: Session, settings: object, node: GraphNode
) -> str:
    view = _node_view(node)
    view["aliases"] = _all_surface_names(session, settings, node)
    payload = json.dumps(
        view, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def judge(node_a: object, node_b: object) -> JudgeResult:
    """Ask the existing chat LLM whether a same-type pair is identical.

    Every transport, parsing, schema, or type error fails closed to
    ``different``.  A model-proposed canonical name is accepted only when it is
    one of the supplied names/aliases.
    """

    type_a = str(getattr(node_a, "type", "") or "")
    type_b = str(getattr(node_b, "type", "") or "")
    if not type_a or type_a != type_b:
        return JudgeResult("different", reason="entity types differ")
    try:
        prompt = (
            _load_prompt()
            .replace(
                "{{entity_a}}",
                json.dumps(_node_view(node_a), ensure_ascii=False, sort_keys=True),
            )
            .replace(
                "{{entity_b}}",
                json.dumps(_node_view(node_b), ensure_ascii=False, sort_keys=True),
            )
        )
        payload = _extract_json(call_llm(prompt))
        decision = str(payload.get("decision", "")).strip().lower()
        if decision not in _DECISIONS:
            raise ValueError(f"invalid decision {decision!r}")
        canonical_name = str(payload.get("canonical_name", "") or "").strip()
        reason = str(payload.get("reason", "") or "").strip()
        if decision != "same":
            canonical_name = ""
        elif canonical_name and not _valid_canonical_name(
            canonical_name, node_a, node_b
        ):
            canonical_name = ""
        return JudgeResult(decision, canonical_name, reason)
    except Exception as exc:  # noqa: BLE001 - resolution must fail closed
        logger.warning("entity judge failed closed: %s", exc)
        return JudgeResult("different", reason=f"judge failed: {exc}")


def _scope_conditions(model: object, settings: object) -> List[object]:
    return [
        model.graph_no == settings.graph_no,
        model.graph_version == settings.graph_version,
    ]


def _load_node(
    session: Session,
    settings: object,
    node_no: str,
    *,
    lock: bool = False,
) -> Optional[GraphNode]:
    stmt = select(GraphNode).where(
        *_scope_conditions(GraphNode, settings),
        GraphNode.graph_node_no == node_no,
    )
    if lock:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _canonical_node(
    session: Session, settings: object, node_no: str, *, lock: bool = False
) -> GraphNode:
    """Follow ``merged_into`` to a root, rejecting missing links and cycles."""

    current_no = node_no
    visited: Set[str] = set()
    while True:
        if current_no in visited:
            chain = " -> ".join([*visited, current_no])
            raise ValueError(f"merged_into cycle detected: {chain}")
        visited.add(current_no)
        node = _load_node(session, settings, current_no, lock=lock)
        if node is None:
            raise ValueError(f"node {current_no!r} does not exist in this graph")
        parent = str(getattr(node, "merged_into", "") or "")
        if not parent:
            if int(getattr(node, "deleted", 0) or 0) != 0:
                raise ValueError(f"node {current_no!r} is deleted without a survivor")
            return node
        current_no = parent


def canonical_node_no(node_no: str) -> str:
    """Resolve a possibly merged node identity to its current survivor."""

    settings = get_settings()
    with session_scope() as session:
        return _canonical_node(session, settings, node_no).graph_node_no


def _aliases_from_table(session: Session, settings: object, node_no: str) -> List[str]:
    rows = (
        session.execute(
            select(NodeAlias.alias).where(
                *_scope_conditions(NodeAlias, settings),
                NodeAlias.canonical_node_no == node_no,
            )
        )
        .scalars()
        .all()
    )
    return [str(value) for value in rows if value]


def _all_surface_names(
    session: Session, settings: object, node: GraphNode
) -> List[str]:
    return _dedupe_strings([*_surface_names(node), *_aliases_from_table(
        session, settings, node.graph_node_no
    )])


def _exact_candidates(
    session: Session, settings: object, node: GraphNode
) -> List[GraphNode]:
    keys = {normalize_alias(name) for name in _all_surface_names(session, settings, node)}
    keys.discard("")
    if not keys:
        return []
    rows = (
        session.execute(
            select(GraphNode)
            .where(
                *_scope_conditions(GraphNode, settings),
                GraphNode.deleted == 0,
                GraphNode.merged_into.is_(None),
                GraphNode.type == node.type,
                GraphNode.graph_node_no != node.graph_node_no,
            )
            .order_by(GraphNode.id, GraphNode.graph_node_no)
        )
        .scalars()
        .all()
    )
    return [
        candidate
        for candidate in rows
        if keys
        & {
            normalized
            for normalized in (
                normalize_alias(name)
                for name in _all_surface_names(session, settings, candidate)
            )
            if normalized
        }
    ]


def _candidate_topk(settings: object) -> int:
    value = getattr(
        settings,
        "resolve_candidate_topk",
        getattr(settings, "candidate_topk", 10),
    )
    return max(1, int(value))


def find_candidates(
    session: Session, node: GraphNode, limit: Optional[int] = None
) -> List[Tuple[GraphNode, float]]:
    """Return same-type live nodes ranked by strict cosine similarity."""

    settings = get_settings()
    topk = _candidate_topk(settings) if limit is None else max(0, int(limit))
    if topk == 0:
        return []
    candidates = (
        session.execute(
            select(GraphNode)
            .where(
                *_scope_conditions(GraphNode, settings),
                GraphNode.deleted == 0,
                GraphNode.merged_into.is_(None),
                GraphNode.type == node.type,
                GraphNode.graph_node_no != node.graph_node_no,
            )
            .order_by(GraphNode.id, GraphNode.graph_node_no)
        )
        .scalars()
        .all()
    )
    if not candidates:
        return []
    vectors = ensure_embeddings([node, *candidates])
    scored = [
        (candidate, cosine(vectors[0], vector))
        for candidate, vector in zip(candidates, vectors[1:])
    ]
    scored.sort(
        key=lambda item: (
            -item[1],
            int(getattr(item[0], "id", 0) or 0),
            item[0].graph_node_no,
        )
    )
    return scored[:topk]


def _json_safe(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("cannot snapshot a non-finite float")
    return value


def _snapshot_row(row: object, fields: Sequence[str]) -> Dict[str, object]:
    return {
        name: _json_safe(getattr(row, name))
        for name in fields
        if hasattr(row, name)
    }


_NODE_FIELDS = (
    "id",
    "graph_no",
    "graph_version",
    "graph_node_no",
    "name",
    "type",
    "description",
    "properties",
    "ref",
    "aliases",
    "merged_into",
    "embedding",
    "embedding_hash",
    "embedding_model",
    "deleted",
    "create_time",
    "update_time",
)
_EDGE_FIELDS = (
    "id",
    "graph_no",
    "graph_version",
    "graph_edge_no",
    "source_node_no",
    "target_node_no",
    "name",
    "description",
    "confidence",
    "properties",
    "ref",
    "deleted",
    "create_time",
    "update_time",
)
_ALIAS_FIELDS = (
    "id",
    "graph_no",
    "graph_version",
    "type",
    "normalized_alias",
    "canonical_node_no",
    "alias",
    "source",
    "create_time",
    "update_time",
)
_NODE_CONTRIB_FIELDS = (
    "id",
    "graph_no",
    "graph_version",
    "doc_key",
    "mention_key",
    "mention_id",
    "canonical_node_no",
    "canonical_rank",
    "name",
    "type",
    "properties",
    "ref",
    "extraction_hash",
    "create_time",
    "update_time",
)
_EDGE_CONTRIB_FIELDS = (
    "id",
    "graph_no",
    "graph_version",
    "doc_key",
    "edge_key",
    "source_mention_key",
    "target_mention_key",
    "source_node_no",
    "target_node_no",
    "name",
    "confidence",
    "properties",
    "ref",
    "extraction_hash",
    "create_time",
    "update_time",
)


def _dedupe_strings(values: Iterable[object]) -> List[str]:
    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _is_gap(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_canonical(winner: object, loser: object) -> object:
    """Recursively fill winner gaps without overwriting canonical values."""

    if not isinstance(winner, dict) or not isinstance(loser, dict):
        return loser if _is_gap(winner) and not _is_gap(loser) else winner
    merged = dict(winner)
    for key, value in loser.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_canonical(merged[key], value)
        elif _is_gap(merged[key]) and not _is_gap(value):
            merged[key] = value
    return merged


def _stable_union(left: Sequence[object], right: Sequence[object]) -> List[object]:
    result: List[object] = []
    seen: Set[str] = set()
    for value in [*left, *right]:
        marker = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    try:
        return sorted(result, key=lambda item: str(item))
    except TypeError:
        return result


def _merge_refs(winner: object, loser: object) -> object:
    if isinstance(winner, dict) and isinstance(loser, dict):
        merged = dict(winner)
        for key, value in loser.items():
            if key not in merged:
                merged[key] = value
            else:
                merged[key] = _merge_refs(merged[key], value)
        return merged
    if isinstance(winner, list) and isinstance(loser, list):
        return _stable_union(winner, loser)
    return loser if _is_gap(winner) else winner


def _more_complete_text(primary: object, secondary: object) -> str:
    first = str(primary or "").strip()
    second = str(secondary or "").strip()
    if len(second) > len(first):
        return second
    return first


def _node_degree(session: Session, settings: object, node_no: str) -> int:
    rows = session.execute(
        select(GraphEdge.source_node_no, GraphEdge.target_node_no).where(
            *_scope_conditions(GraphEdge, settings),
            GraphEdge.deleted == 0,
            or_(
                GraphEdge.source_node_no == node_no,
                GraphEdge.target_node_no == node_no,
            ),
        )
    ).all()
    return len(rows)


def _time_key(value: object) -> Tuple[int, str]:
    if isinstance(value, (datetime, date)):
        return (0, value.isoformat())
    if value is None:
        return (1, "")
    return (0, str(value))


def _select_survivor(
    session: Session, settings: object, node_a: GraphNode, node_b: GraphNode
) -> Tuple[GraphNode, GraphNode]:
    """Choose a stable survivor.

    An earlier creation timestamp is treated as an older persisted node and has
    priority.  Nodes from the same creation batch are ranked by live degree,
    then earlier update time, numeric id and finally business key.
    """

    created_a = _time_key(getattr(node_a, "create_time", None))
    created_b = _time_key(getattr(node_b, "create_time", None))
    if created_a != created_b:
        return (node_a, node_b) if created_a < created_b else (node_b, node_a)

    degree_a = _node_degree(session, settings, node_a.graph_node_no)
    degree_b = _node_degree(session, settings, node_b.graph_node_no)
    key_a = (
        -degree_a,
        _time_key(getattr(node_a, "update_time", None)),
        int(getattr(node_a, "id", 0) or 0),
        node_a.graph_node_no,
    )
    key_b = (
        -degree_b,
        _time_key(getattr(node_b, "update_time", None)),
        int(getattr(node_b, "id", 0) or 0),
        node_b.graph_node_no,
    )
    return (node_a, node_b) if key_a <= key_b else (node_b, node_a)


def _edge_no(source_no: str, label: str, target_no: str) -> str:
    raw = f"{source_no}|{label}|{target_no}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _confidence(edge: GraphEdge) -> str:
    direct = getattr(edge, "confidence", None)
    if direct:
        return str(direct).upper()
    return str((getattr(edge, "properties", None) or {}).get("confidence", "")).upper()


def _merge_edge_payload(survivor: GraphEdge, others: Sequence[GraphEdge]) -> None:
    props = dict(getattr(survivor, "properties", None) or {})
    ref = getattr(survivor, "ref", None) or {}
    description = str(getattr(survivor, "description", "") or "")
    confidence = _confidence(survivor)
    for edge in others:
        props = _merge_canonical(props, getattr(edge, "properties", None) or {})
        ref = _merge_refs(ref, getattr(edge, "ref", None) or {})
        description = _more_complete_text(
            description, getattr(edge, "description", "")
        )
        candidate = _confidence(edge)
        if _CONFIDENCE_RANK.get(candidate, -1) > _CONFIDENCE_RANK.get(
            confidence, -1
        ):
            confidence = candidate
    if confidence:
        props["confidence"] = confidence
        if hasattr(survivor, "confidence"):
            survivor.confidence = confidence
    survivor.properties = props
    survivor.ref = ref
    if hasattr(survivor, "description"):
        survivor.description = description or None


def _normalize_edges(
    session: Session, settings: object, winner_no: str, loser_no: str, merge_id: str
) -> List[Dict[str, object]]:
    """Re-point loser edges and normalize only resulting edge triples.

    Unrelated pre-existing self-loops or duplicates are deliberately outside
    this merge's mutation/audit boundary.  We additionally lock rows that may
    collide with a remapped edge by triple or ``graph_edge_no``.
    """

    affected = (
        session.execute(
            select(GraphEdge)
            .where(
                *_scope_conditions(GraphEdge, settings),
                GraphEdge.deleted == 0,
                or_(
                    GraphEdge.source_node_no == loser_no,
                    GraphEdge.target_node_no == loser_no,
                ),
            )
            .order_by(GraphEdge.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if not affected:
        return []

    projected: Dict[int, Tuple[str, str, str]] = {}
    desired_keys: Set[Tuple[str, str, str]] = set()
    for edge in affected:
        source = winner_no if edge.source_node_no == loser_no else edge.source_node_no
        target = winner_no if edge.target_node_no == loser_no else edge.target_node_no
        key = (source, target, edge.name)
        projected[edge.id] = key
        if source == target:
            continue
        desired_keys.add(key)

    desired_nos = {_edge_no(source, label, target) for source, target, label in desired_keys}
    collision_conditions = [
        and_(
            GraphEdge.source_node_no == source,
            GraphEdge.target_node_no == target,
            GraphEdge.name == label,
        )
        for source, target, label in desired_keys
    ]
    if desired_nos:
        collision_conditions.append(GraphEdge.graph_edge_no.in_(desired_nos))
    collisions: List[GraphEdge] = []
    if collision_conditions:
        collisions = (
            session.execute(
                select(GraphEdge)
                .where(
                    *_scope_conditions(GraphEdge, settings),
                    or_(*collision_conditions),
                )
                .order_by(GraphEdge.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )

    relevant = {edge.id: edge for edge in [*affected, *collisions]}
    affected_ids = {edge.id for edge in affected}
    groups: Dict[Tuple[str, str, str], List[GraphEdge]] = {
        key: [] for key in desired_keys
    }
    for edge in affected:
        key = projected[edge.id]
        if key in groups:
            groups[key].append(edge)
    for edge in collisions:
        actual = (edge.source_node_no, edge.target_node_no, edge.name)
        if edge.id not in affected_ids and int(edge.deleted or 0) == 0 and actual in groups:
            groups[actual].append(edge)

    final_survivors: List[Tuple[GraphEdge, Tuple[str, str, str], List[GraphEdge]]] = []
    duplicate_ids: Set[int] = set()
    payload_survivor_ids: Set[int] = set()
    for key, members in groups.items():
        desired_no = _edge_no(key[0], key[2], key[1])
        unique_members = {edge.id: edge for edge in members}
        members = list(unique_members.values())
        members.sort(
            key=lambda edge: (
                0
                if (
                    edge.source_node_no == key[0]
                    and edge.target_node_no == key[1]
                    and edge.graph_edge_no == desired_no
                )
                else 1,
                0 if edge.id not in affected_ids else 1,
                int(edge.id or 0),
                edge.graph_edge_no,
            )
        )
        survivor, duplicates = members[0], members[1:]
        duplicate_ids.update(edge.id for edge in duplicates)
        if duplicates:
            payload_survivor_ids.add(survivor.id)
        final_survivors.append((survivor, key, duplicates))

    final_owner = {
        _edge_no(key[0], key[2], key[1]): survivor.id
        for survivor, key, _duplicates in final_survivors
    }
    blocker_ids: Set[int] = set()
    for edge in collisions:
        owner_id = final_owner.get(edge.graph_edge_no)
        if owner_id is None or owner_id == edge.id:
            continue
        actual = (edge.source_node_no, edge.target_node_no, edge.name)
        if (
            int(edge.deleted or 0) == 0
            and edge.id not in affected_ids
            and actual not in desired_keys
        ):
            raise ValueError(
                f"edge key collision on corrupt live edge {edge.graph_edge_no!r}"
            )
        blocker_ids.add(edge.id)

    self_loop_ids = {
        edge.id for edge in affected if projected[edge.id][0] == projected[edge.id][1]
    }
    mutation_ids = affected_ids | duplicate_ids | blocker_ids | payload_survivor_ids
    snapshots = [
        _snapshot_row(relevant[edge_id], _EDGE_FIELDS)
        for edge_id in sorted(mutation_ids)
    ]

    new_no_by_id = {
        survivor.id: _edge_no(key[0], key[2], key[1])
        for survivor, key, _duplicates in final_survivors
    }
    for edge_id in mutation_ids:
        edge = relevant[edge_id]
        owns_final_no = new_no_by_id.get(edge_id) == edge.graph_edge_no
        if edge_id in duplicate_ids or edge_id in self_loop_ids:
            edge.graph_edge_no = f"tmp:{merge_id[:16]}:{edge.id}"
        elif edge.graph_edge_no in final_owner and not owns_final_no:
            edge.graph_edge_no = f"tmp:{merge_id[:16]}:{edge.id}"
        elif edge_id in new_no_by_id and edge.graph_edge_no != new_no_by_id[edge_id]:
            edge.graph_edge_no = f"tmp:{merge_id[:16]}:{edge.id}"
    session.flush()

    for edge in affected:
        source, target, _label = projected[edge.id]
        edge.source_node_no = source
        edge.target_node_no = target
        if edge.id in self_loop_ids or edge.id in duplicate_ids:
            edge.deleted = 1

    for survivor, key, duplicates in final_survivors:
        source, target, label = key
        _merge_edge_payload(survivor, duplicates)
        survivor.source_node_no = source
        survivor.target_node_no = target
        survivor.graph_edge_no = _edge_no(source, label, target)
        survivor.deleted = 0
        for duplicate in duplicates:
            duplicate.deleted = 1
    return snapshots


def _query_alias_rows(
    session: Session, settings: object, node_nos: Sequence[str], *, lock: bool = False
) -> List[NodeAlias]:
    stmt = (
        select(NodeAlias)
        .where(
            *_scope_conditions(NodeAlias, settings),
            NodeAlias.canonical_node_no.in_(list(node_nos)),
        )
        .order_by(NodeAlias.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(session.execute(stmt).scalars().all())


def _replace_alias_rows(
    session: Session,
    settings: object,
    winner: GraphNode,
    loser: GraphNode,
    names: Sequence[str],
) -> List[Dict[str, object]]:
    existing = _query_alias_rows(
        session, settings, [winner.graph_node_no, loser.graph_node_no], lock=True
    )
    snapshots = [_snapshot_row(row, _ALIAS_FIELDS) for row in existing]
    for row in existing:
        session.delete(row)
    session.flush()

    # One display spelling per normalized alias is enough for indexed blocking;
    # GraphNode.aliases retains the complete surface-name union.
    by_normalized: Dict[str, str] = {}
    for name in names:
        normalized = normalize_alias(name)
        if not normalized:
            continue
        current = by_normalized.get(normalized)
        by_normalized[normalized] = (
            _more_complete_text(current, name) if current else str(name).strip()
        )
    for normalized, alias in sorted(by_normalized.items()):
        session.add(
            NodeAlias(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                type=winner.type,
                normalized_alias=normalized,
                canonical_node_no=winner.graph_node_no,
                alias=alias,
                source="merge",
            )
        )
    return snapshots


def _update_contributions(
    session: Session, settings: object, winner_no: str, loser_no: str
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    node_rows = list(
        session.execute(
            select(NodeContribution)
            .where(
                *_scope_conditions(NodeContribution, settings),
                NodeContribution.canonical_node_no.in_([winner_no, loser_no]),
            )
            .order_by(NodeContribution.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    node_snaps = [_snapshot_row(row, _NODE_CONTRIB_FIELDS) for row in node_rows]
    winner_rows = [row for row in node_rows if row.canonical_node_no == winner_no]
    loser_rows = [row for row in node_rows if row.canonical_node_no == loser_no]
    winner_max_rank = max(
        (int(getattr(row, "canonical_rank", 0) or 0) for row in winner_rows),
        default=-1,
    )
    loser_min_rank = min(
        (int(getattr(row, "canonical_rank", 0) or 0) for row in loser_rows),
        default=0,
    )
    for row in node_rows:
        if row.canonical_node_no == loser_no:
            row.canonical_node_no = winner_no
            # Keep every winner contribution's precedence unchanged.  Shift
            # the loser block behind it while preserving the loser's internal
            # relative ranks.  Store aggregation then remains canonical-wins.
            row.canonical_rank = (
                winner_max_rank
                + 1
                + int(getattr(row, "canonical_rank", 0) or 0)
                - loser_min_rank
            )

    edge_rows = list(
        session.execute(
            select(EdgeContribution)
            .where(
                *_scope_conditions(EdgeContribution, settings),
                or_(
                    EdgeContribution.source_node_no == winner_no,
                    EdgeContribution.target_node_no == winner_no,
                    EdgeContribution.source_node_no == loser_no,
                    EdgeContribution.target_node_no == loser_no,
                ),
            )
            .order_by(EdgeContribution.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    edge_snaps = [_snapshot_row(row, _EDGE_CONTRIB_FIELDS) for row in edge_rows]
    for row in edge_rows:
        if row.source_node_no == loser_no:
            row.source_node_no = winner_no
        if row.target_node_no == loser_no:
            row.target_node_no = winner_no
    return node_snaps, edge_snaps


def merge(
    node_a_no: str,
    node_b_no: str,
    *,
    canonical_name: str = "",
    reason: str = "",
    score: Optional[float] = None,
    expected_roots: Optional[Tuple[str, str]] = None,
    expected_fingerprints: Optional[Dict[str, str]] = None,
    _session: Optional[Session] = None,
) -> str:
    """Atomically merge two identities and return the audit ``merge_id``.

    Both inputs are canonicalized under row locks.  The survivor is selected
    internally, so caller order cannot change the result.
    """

    settings = get_settings()
    if score is not None and not math.isfinite(float(score)):
        raise ValueError("merge score must be finite")
    merge_id = uuid.uuid4().hex
    scope = session_scope() if _session is None else nullcontext(_session)
    with scope as session:
        acquire_graph_write_lock(
            session, settings.graph_no, settings.graph_version
        )
        # Acquire the initial pair in stable order to reduce deadlock risk, then
        # follow any redirects while holding locks.
        for node_no in sorted({node_a_no, node_b_no}):
            if _load_node(session, settings, node_no, lock=True) is None:
                raise ValueError(f"node {node_no!r} does not exist in this graph")
        node_a = _canonical_node(session, settings, node_a_no, lock=True)
        node_b = _canonical_node(session, settings, node_b_no, lock=True)
        if node_a.graph_node_no == node_b.graph_node_no:
            raise ValueError("both inputs already resolve to the same survivor")
        actual_roots = tuple(
            sorted((node_a.graph_node_no, node_b.graph_node_no))
        )
        if expected_roots is not None and actual_roots != tuple(
            sorted(expected_roots)
        ):
            raise ValueError(
                "judged pair changed before merge; candidates must be regenerated"
            )
        if expected_fingerprints is not None:
            for current in (node_a, node_b):
                expected = expected_fingerprints.get(current.graph_node_no)
                if (
                    expected is None
                    or _persistent_decision_fingerprint(session, settings, current)
                    != expected
                ):
                    raise ValueError(
                        "judged entity changed before merge; candidates must be regenerated"
                    )
        if node_a.type != node_b.type:
            raise ValueError("cannot merge nodes of different types")
        winner, loser = _select_survivor(session, settings, node_a, node_b)

        winner_before = _snapshot_row(winner, _NODE_FIELDS)
        loser_before = _snapshot_row(loser, _NODE_FIELDS)
        all_names = _dedupe_strings(
            [
                *_all_surface_names(session, settings, winner),
                *_all_surface_names(session, settings, loser),
            ]
        )
        alias_before = _replace_alias_rows(
            session, settings, winner, loser, all_names
        )
        node_contrib_before, edge_contrib_before = _update_contributions(
            session, settings, winner.graph_node_no, loser.graph_node_no
        )
        edge_before = _normalize_edges(
            session, settings, winner.graph_node_no, loser.graph_node_no, merge_id
        )

        winner.properties = _merge_canonical(
            winner.properties or {}, loser.properties or {}
        )
        winner.ref = _merge_refs(winner.ref or {}, loser.ref or {})
        if hasattr(winner, "description"):
            winner.description = (
                _more_complete_text(
                    getattr(winner, "description", ""),
                    getattr(loser, "description", ""),
                )
                or None
            )
        selected_name = (
            str(canonical_name).strip()
            if _valid_canonical_name(canonical_name, winner, loser)
            else _more_complete_text(winner.name, loser.name)
        )
        winner.name = selected_name or winner.name
        winner.aliases = _dedupe_strings([winner.name, *all_names])
        winner.merged_into = None
        winner.deleted = 0
        winner.embedding = None
        winner.embedding_hash = None
        winner.embedding_model = None

        loser.deleted = 1
        loser.merged_into = winner.graph_node_no
        loser.embedding = None
        loser.embedding_hash = None
        loser.embedding_model = None

        session.flush()
        alias_after = [
            _snapshot_row(row, _ALIAS_FIELDS)
            for row in _query_alias_rows(
                session,
                settings,
                [winner.graph_node_no, loser.graph_node_no],
                lock=True,
            )
        ]
        edge_after = [
            _snapshot_row(session.get(GraphEdge, row["id"]), _EDGE_FIELDS)
            for row in edge_before
        ]
        node_contrib_after = [
            _snapshot_row(
                session.get(NodeContribution, row["id"]),
                _NODE_CONTRIB_FIELDS,
            )
            for row in node_contrib_before
        ]
        edge_contrib_after = [
            _snapshot_row(
                session.get(EdgeContribution, row["id"]),
                _EDGE_CONTRIB_FIELDS,
            )
            for row in edge_contrib_before
        ]
        incident_edge_ids_after = list(
            session.execute(
                select(GraphEdge.id)
                .where(
                    *_scope_conditions(GraphEdge, settings),
                    or_(
                        GraphEdge.source_node_no.in_(
                            [winner.graph_node_no, loser.graph_node_no]
                        ),
                        GraphEdge.target_node_no.in_(
                            [winner.graph_node_no, loser.graph_node_no]
                        ),
                    ),
                )
                .order_by(GraphEdge.id)
            ).scalars()
        )
        snapshot: Dict[str, object] = {
            "version": 1,
            "reason": str(reason or ""),
            "score": score,
            "winner": winner_before,
            "loser": loser_before,
            "aliases": alias_before,
            "edges": edge_before,
            "node_contributions": node_contrib_before,
            "edge_contributions": edge_contrib_before,
            "after": {
                "winner": _snapshot_row(winner, _NODE_FIELDS),
                "loser": _snapshot_row(loser, _NODE_FIELDS),
                "aliases": alias_after,
                "edges": edge_after,
                "node_contributions": node_contrib_after,
                "edge_contributions": edge_contrib_after,
                "incident_edge_ids": incident_edge_ids_after,
            },
        }
        session.add(
            MergeLog(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                merge_id=merge_id,
                winner_node_no=winner.graph_node_no,
                loser_node_no=loser.graph_node_no,
                snapshot=snapshot,
                status="MERGED",
            )
        )
    return merge_id


def _restore_value(model: object, field_name: str, value: object) -> object:
    column = getattr(getattr(model, "__table__", None), "columns", {}).get(
        field_name
    )
    if column is not None and value is not None:
        try:
            from sqlalchemy import DateTime

            if isinstance(column.type, DateTime) and isinstance(value, str):
                return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            pass
    return value


def _restore_row(row: object, snapshot: Dict[str, object]) -> None:
    for field_name, value in snapshot.items():
        if field_name == "id" or not hasattr(row, field_name):
            continue
        setattr(row, field_name, _restore_value(type(row), field_name, value))


def _restore_by_id(
    session: Session, model: object, snapshots: Sequence[Dict[str, object]]
) -> None:
    for snapshot in snapshots:
        row_id = snapshot.get("id")
        row = session.get(model, row_id) if row_id is not None else None
        if row is None:
            raise ValueError(f"cannot rollback: {model.__name__} id={row_id} is missing")
        _restore_row(row, snapshot)


def _restore_aliases(
    session: Session,
    settings: object,
    node_nos: Sequence[str],
    snapshots: Sequence[Dict[str, object]],
) -> None:
    current = _query_alias_rows(session, settings, node_nos, lock=True)
    for row in current:
        session.delete(row)
    session.flush()
    for snapshot in snapshots:
        values = {
            field_name: _restore_value(NodeAlias, field_name, value)
            for field_name, value in snapshot.items()
            if hasattr(NodeAlias, field_name)
        }
        session.add(NodeAlias(**values))


def _snapshot_ids(snapshot: Dict[str, object], key: str) -> Set[object]:
    rows = snapshot.get(key) or []
    if not isinstance(rows, list):
        return set()
    return {row.get("id") for row in rows if isinstance(row, dict)}


def _normalized_snapshots(rows: Sequence[Dict[str, object]]) -> List[str]:
    # SQLAlchemy's client/server ``onupdate`` timestamp can be refreshed only
    # after the merge transaction commits.  Semantic fields and row identity
    # provide the concurrency guard; volatile update timestamps do not.
    stable_rows = [
        {
            key: value
            for key, value in row.items()
            if key
            not in {
                "update_time",
                # Embeddings are a derived cache.  Lazy refresh after merge is
                # safe and must not make the semantic operation unrollable.
                "embedding",
                "embedding_hash",
                "embedding_model",
            }
        }
        for row in rows
    ]
    return sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in stable_rows
    )


def _assert_rows_match_after(
    session: Session,
    model: object,
    fields: Sequence[str],
    snapshots: Sequence[Dict[str, object]],
    label: str,
) -> None:
    current: List[Dict[str, object]] = []
    for expected in snapshots:
        row = session.get(model, expected.get("id"))
        if row is None:
            raise ValueError(f"cannot rollback: {label} row is missing")
        current.append(_snapshot_row(row, fields))
    if _normalized_snapshots(current) != _normalized_snapshots(list(snapshots)):
        raise ValueError(f"cannot rollback: {label} changed after the merge")


def _assert_merge_state_unchanged(
    session: Session,
    settings: object,
    log: MergeLog,
    snapshot: Dict[str, object],
    winner: GraphNode,
    loser: GraphNode,
) -> None:
    after = snapshot.get("after") or {}
    if not isinstance(after, dict):
        raise ValueError("cannot rollback: merge snapshot has no after-state")
    expected_nodes = [after.get("winner") or {}, after.get("loser") or {}]
    current_nodes = [
        _snapshot_row(winner, _NODE_FIELDS),
        _snapshot_row(loser, _NODE_FIELDS),
    ]
    if _normalized_snapshots(current_nodes) != _normalized_snapshots(expected_nodes):
        raise ValueError("cannot rollback: winner or loser changed after the merge")
    _assert_rows_match_after(
        session, GraphEdge, _EDGE_FIELDS, after.get("edges") or [], "edge"
    )
    _assert_rows_match_after(
        session,
        NodeContribution,
        _NODE_CONTRIB_FIELDS,
        after.get("node_contributions") or [],
        "node contribution",
    )
    _assert_rows_match_after(
        session,
        EdgeContribution,
        _EDGE_CONTRIB_FIELDS,
        after.get("edge_contributions") or [],
        "edge contribution",
    )
    current_aliases = [
        _snapshot_row(row, _ALIAS_FIELDS)
        for row in _query_alias_rows(
            session,
            settings,
            [log.winner_node_no, log.loser_node_no],
            lock=True,
        )
    ]
    if _normalized_snapshots(current_aliases) != _normalized_snapshots(
        after.get("aliases") or []
    ):
        raise ValueError("cannot rollback: aliases changed after the merge")

    after_edge_ids = {value for value in after.get("incident_edge_ids") or []}
    current_edge_ids = set(
        session.execute(
            select(GraphEdge.id).where(
                *_scope_conditions(GraphEdge, settings),
                or_(
                    GraphEdge.source_node_no.in_(
                        [log.winner_node_no, log.loser_node_no]
                    ),
                    GraphEdge.target_node_no.in_(
                        [log.winner_node_no, log.loser_node_no]
                    ),
                ),
            )
        ).scalars()
    )
    if current_edge_ids != after_edge_ids:
        raise ValueError("cannot rollback: incident edges changed after the merge")

    after_node_contrib_ids = _snapshot_ids(after, "node_contributions")
    current_node_contrib_ids = set(
        session.execute(
            select(NodeContribution.id).where(
                *_scope_conditions(NodeContribution, settings),
                NodeContribution.canonical_node_no.in_(
                    [log.winner_node_no, log.loser_node_no]
                ),
            )
        ).scalars()
    )
    if current_node_contrib_ids != after_node_contrib_ids:
        raise ValueError(
            "cannot rollback: node contributions changed after the merge"
        )

    after_edge_contrib_ids = _snapshot_ids(after, "edge_contributions")
    current_edge_contrib_ids = set(
        session.execute(
            select(EdgeContribution.id).where(
                *_scope_conditions(EdgeContribution, settings),
                or_(
                    EdgeContribution.source_node_no.in_(
                        [log.winner_node_no, log.loser_node_no]
                    ),
                    EdgeContribution.target_node_no.in_(
                        [log.winner_node_no, log.loser_node_no]
                    ),
                ),
            )
        ).scalars()
    )
    if current_edge_contrib_ids != after_edge_contrib_ids:
        raise ValueError(
            "cannot rollback: edge contributions changed after the merge"
        )

    before_edge_nos = {
        row.get("graph_edge_no")
        for row in (snapshot.get("edges") or [])
        if isinstance(row, dict) and row.get("graph_edge_no")
    }
    affected_ids = _snapshot_ids(snapshot, "edges")
    if before_edge_nos:
        conflict = session.execute(
            select(GraphEdge.id).where(
                *_scope_conditions(GraphEdge, settings),
                GraphEdge.graph_edge_no.in_(before_edge_nos),
                ~GraphEdge.id.in_(affected_ids),
            )
        ).scalars().first()
        if conflict is not None:
            raise ValueError("cannot rollback: a new edge occupies a restored key")


def _assert_no_dependent_merge(
    session: Session, settings: object, log: MergeLog, snapshot: Dict[str, object]
) -> None:
    """Reject rollback when a later active merge consumed its state."""

    later_logs = (
        session.execute(
            select(MergeLog)
            .where(
                *_scope_conditions(MergeLog, settings),
                MergeLog.status == "MERGED",
                MergeLog.id > log.id,
            )
            .order_by(MergeLog.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    current_nodes = {log.winner_node_no, log.loser_node_no}
    current_ids = {
        key: _snapshot_ids(snapshot, key)
        for key in ("edges", "node_contributions", "edge_contributions")
    }
    for later in later_logs:
        if current_nodes & {later.winner_node_no, later.loser_node_no}:
            raise ValueError(
                f"cannot rollback before dependent merge {later.merge_id!r}"
            )
        later_snapshot = later.snapshot or {}
        if any(
            current_ids[key] & _snapshot_ids(later_snapshot, key)
            for key in current_ids
        ):
            raise ValueError(
                f"cannot rollback before overlapping merge {later.merge_id!r}"
            )


def rollback(merge_id: str) -> None:
    """Restore a merge's full before-images, or raise ``ValueError``."""

    settings = get_settings()
    with session_scope() as session:
        acquire_graph_write_lock(
            session, settings.graph_no, settings.graph_version
        )
        log = session.execute(
            select(MergeLog)
            .where(
                *_scope_conditions(MergeLog, settings),
                MergeLog.merge_id == merge_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if log is None:
            raise ValueError(f"no merge with id {merge_id!r}")
        if log.status != "MERGED":
            raise ValueError(f"merge {merge_id!r} is not active ({log.status})")
        snapshot = log.snapshot or {}
        _assert_no_dependent_merge(session, settings, log, snapshot)
        winner = _load_node(session, settings, log.winner_node_no, lock=True)
        loser = _load_node(session, settings, log.loser_node_no, lock=True)
        if winner is None or loser is None:
            raise ValueError("cannot rollback: winner or loser row is missing")
        if winner.merged_into:
            raise ValueError("cannot rollback after the winner was merged again")
        if loser.merged_into != winner.graph_node_no:
            raise ValueError("cannot rollback: loser no longer points to this winner")
        _assert_merge_state_unchanged(
            session, settings, log, snapshot, winner, loser
        )

        edge_snapshots = snapshot.get("edges") or []
        # Free edge_no values before restoring them; this also makes swaps safe.
        for edge_snapshot in edge_snapshots:
            edge = session.get(GraphEdge, edge_snapshot.get("id"))
            if edge is None:
                raise ValueError("cannot rollback: an edge row is missing")
            edge.graph_edge_no = f"tmp:rollback:{uuid.uuid4().hex[:12]}:{edge.id}"
        session.flush()
        _restore_by_id(session, GraphEdge, edge_snapshots)
        _restore_by_id(
            session, NodeContribution, snapshot.get("node_contributions") or []
        )
        _restore_by_id(
            session, EdgeContribution, snapshot.get("edge_contributions") or []
        )
        _restore_aliases(
            session,
            settings,
            [winner.graph_node_no, loser.graph_node_no],
            snapshot.get("aliases") or [],
        )
        _restore_row(winner, snapshot.get("winner") or {})
        _restore_row(loser, snapshot.get("loser") or {})
        log.status = "ROLLED_BACK"


def rollback_merge(merge_id: str) -> None:
    """Backward-friendly alias for :func:`rollback`."""

    rollback(merge_id)


def alias_of(canonical_name_or_no: str) -> List[str]:
    """List surface aliases for a live canonical node by id or name."""

    settings = get_settings()
    with session_scope() as session:
        node = _load_node(session, settings, canonical_name_or_no)
        if node is not None:
            node = _canonical_node(session, settings, node.graph_node_no)
        else:
            rows = (
                session.execute(
                    select(GraphNode)
                    .where(
                        *_scope_conditions(GraphNode, settings),
                        GraphNode.deleted == 0,
                        GraphNode.merged_into.is_(None),
                    )
                    .order_by(GraphNode.id)
                )
                .scalars()
                .all()
            )
            wanted = normalize_alias(canonical_name_or_no)
            node = next(
                (
                    row
                    for row in rows
                    if wanted
                    and wanted
                    in {
                        normalize_alias(name)
                        for name in _all_surface_names(session, settings, row)
                    }
                ),
                None,
            )
        if node is None:
            return []
        return [
            alias
            for alias in _all_surface_names(session, settings, node)
            if alias != node.name
        ]


def _thresholds(settings: object) -> Tuple[float, float]:
    low = float(getattr(settings, "resolve_low_threshold", 0.75))
    high = float(getattr(settings, "resolve_high_threshold", 0.92))
    if not math.isfinite(low) or not math.isfinite(high) or not 0 <= low < high <= 1:
        raise ValueError("resolve thresholds must satisfy 0 <= LOW < HIGH <= 1")
    return low, high


def _scan_node_nos(
    session: Session, settings: object, type_filter: Optional[str], limit: Optional[int]
) -> List[str]:
    stmt = (
        select(GraphNode.graph_node_no)
        .where(
            *_scope_conditions(GraphNode, settings),
            GraphNode.deleted == 0,
            GraphNode.merged_into.is_(None),
        )
        .order_by(GraphNode.create_time, GraphNode.id, GraphNode.graph_node_no)
    )
    if type_filter:
        stmt = stmt.where(GraphNode.type == type_filter)
    if limit is not None:
        stmt = stmt.limit(max(0, int(limit)))
    return list(session.execute(stmt).scalars().all())


def _resolve_all_dry_run(
    settings: object,
    low: float,
    high: float,
    type_filter: Optional[str],
    limit: Optional[int],
) -> ResolveStats:
    """Execute the real merge sequence in one transaction, then roll it back.

    This makes dry-run merge counts and survivor/candidate reload behavior match
    a real run (for example, an exact-match clique of three nodes yields two
    merges, not three pair proposals) while guaranteeing zero durable writes,
    including lazily generated embedding cache rows and MergeLog records.
    """

    stats = ResolveStats()
    processed_pairs: Set[Tuple[str, str]] = set()
    with session_scope() as session:
        try:
            seed_nos = _scan_node_nos(session, settings, type_filter, limit)
            for initial_no in seed_nos:
                counted = False
                while True:
                    try:
                        node = _canonical_node(session, settings, initial_no)
                        if type_filter and node.type != type_filter:
                            break
                        if not counted:
                            stats.scanned += 1
                            counted = True
                        exact = _exact_candidates(session, settings, node)
                        if exact:
                            candidates: List[
                                Tuple[GraphNode, Optional[float], str]
                            ] = [
                                (candidate, None, "exact")
                                for candidate in exact
                            ]
                        else:
                            candidates = [
                                (candidate, score, "vector")
                                for candidate, score in find_candidates(session, node)
                            ]
                        fingerprints = {
                            current.graph_node_no: _persistent_decision_fingerprint(
                                session, settings, current
                            )
                            for current in [
                                node,
                                *(
                                    candidate
                                    for candidate, _score, _channel in candidates
                                ),
                            ]
                        }
                    except Exception as exc:  # noqa: BLE001 - isolate one seed
                        stats.failed += 1
                        stats.judgments.append(f"FAILED {initial_no}: {exc}")
                        break

                    merged_this_round = False
                    retry_with_fresh_candidates = False
                    for candidate, score, channel in candidates:
                        pair = tuple(
                            sorted((node.graph_node_no, candidate.graph_node_no))
                        )
                        if pair in processed_pairs:
                            continue
                        processed_pairs.add(pair)
                        stats.pairs += 1

                        decision = "different"
                        canonical_name = ""
                        reason = ""
                        bucket = channel
                        if channel == "exact":
                            decision = "same"
                            reason = "normalized name/alias exact match"
                            canonical_name = _more_complete_text(
                                node.name, candidate.name
                            )
                        elif score is not None and score >= high:
                            decision = "same"
                            reason = f"cosine {score:.6f} >= HIGH {high:.6f}"
                            canonical_name = _more_complete_text(
                                node.name, candidate.name
                            )
                            bucket = "high"
                        elif score is not None and score >= low:
                            result = judge(node, candidate)
                            decision = result.decision
                            canonical_name = result.canonical_name
                            reason = result.reason
                            bucket = "llm"
                        else:
                            reason = (
                                f"cosine {float(score or 0):.6f} < LOW {low:.6f}"
                            )

                        score_text = (
                            "" if score is None else f" score={score:.6f}"
                        )
                        stats.judgments.append(
                            f"DRY-RUN {bucket.upper()} {pair[0]} <-> {pair[1]}: "
                            f"{decision}{score_text} ({reason})"
                        )
                        if decision != "same":
                            stats.skipped += 1
                            continue

                        try:
                            merge(
                                node.graph_node_no,
                                candidate.graph_node_no,
                                canonical_name=canonical_name,
                                reason=reason,
                                score=score,
                                expected_roots=pair,
                                expected_fingerprints={
                                    node.graph_node_no: fingerprints[
                                        node.graph_node_no
                                    ],
                                    candidate.graph_node_no: fingerprints[
                                        candidate.graph_node_no
                                    ],
                                },
                                _session=session,
                            )
                            if bucket == "exact":
                                stats.exact_merged += 1
                            elif bucket == "high":
                                stats.high_merged += 1
                            else:
                                stats.llm_merged += 1
                            merged_this_round = True
                            break
                        except ValueError as exc:
                            if "candidates must be regenerated" in str(exc):
                                processed_pairs.discard(pair)
                                retry_with_fresh_candidates = True
                                stats.judgments.append(
                                    f"DRY-RUN RETRY {pair[0]} <-> {pair[1]}: {exc}"
                                )
                                break
                            stats.failed += 1
                            stats.judgments.append(
                                f"dry-run merge skipped for {pair}: {exc}"
                            )

                    if retry_with_fresh_candidates:
                        continue
                    if not merged_this_round:
                        break
        finally:
            # session_scope will issue a harmless commit after this rollback.
            # No simulated node/edge/alias/contribution/log/cache write survives.
            session.rollback()
    return stats


def resolve_all(
    type_filter: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> ResolveStats:
    """Resolve live nodes using exact blocking, vector gates, then LLM.

    ``limit`` limits seed nodes, not the per-seed candidate top-k.  Each direct
    canonical pair is considered at most once in this run.  Successful merges
    are committed immediately and candidates are then recomputed from the new
    survivor, preventing stale batch/union-find propagation.
    """

    settings = get_settings()
    low, high = _thresholds(settings)
    if dry_run:
        return _resolve_all_dry_run(settings, low, high, type_filter, limit)
    stats = ResolveStats()
    processed_pairs: Set[Tuple[str, str]] = set()
    with session_scope() as session:
        seed_nos = _scan_node_nos(session, settings, type_filter, limit)

    for initial_no in seed_nos:
        counted = False
        while True:
            try:
                with session_scope() as session:
                    if not dry_run:
                        acquire_graph_write_lock(
                            session, settings.graph_no, settings.graph_version
                        )
                    # In dry-run, lazy embedding generation may dirty node ORM
                    # objects.  Suppress autoflush across *all* subsequent
                    # candidate/fingerprint queries, then detach those objects
                    # before session_scope can commit.
                    no_flush = session.no_autoflush if dry_run else nullcontext()
                    with no_flush:
                        node = _canonical_node(session, settings, initial_no)
                        if type_filter and node.type != type_filter:
                            break
                        if not counted:
                            stats.scanned += 1
                            counted = True
                        exact = _exact_candidates(session, settings, node)
                        if exact:
                            candidates: List[
                                Tuple[GraphNode, Optional[float], str]
                            ] = [
                                (candidate, None, "exact")
                                for candidate in exact
                            ]
                        else:
                            candidates = [
                                (candidate, score, "vector")
                                for candidate, score in find_candidates(session, node)
                            ]
                        fingerprints = {
                            current.graph_node_no: _persistent_decision_fingerprint(
                                session, settings, current
                            )
                            for current in [
                                node,
                                *(
                                    candidate
                                    for candidate, _score, _channel in candidates
                                ),
                            ]
                        }
                    # A dry run must not persist lazily generated embeddings.
                    # Expunging preserves loaded values for the decision below
                    # while leaving the session with nothing dirty to commit.
                    if dry_run:
                        session.expunge_all()
            except ValueError as exc:
                # A seed absorbed earlier in this run is normal; malformed
                # redirects/embeddings are surfaced in stats but do not abort all.
                stats.failed += 1
                stats.judgments.append(f"FAILED {initial_no}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - isolate one bad seed/provider
                stats.failed += 1
                stats.judgments.append(f"FAILED {initial_no}: {exc}")
                break

            merged_this_round = False
            retry_with_fresh_candidates = False
            for candidate, score, channel in candidates:
                pair = tuple(sorted((node.graph_node_no, candidate.graph_node_no)))
                if pair in processed_pairs:
                    continue
                processed_pairs.add(pair)
                stats.pairs += 1

                decision = "different"
                canonical_name = ""
                reason = ""
                bucket = channel
                if channel == "exact":
                    decision = "same"
                    reason = "normalized name/alias exact match"
                    canonical_name = _more_complete_text(node.name, candidate.name)
                elif score is not None and score >= high:
                    decision = "same"
                    reason = f"cosine {score:.6f} >= HIGH {high:.6f}"
                    canonical_name = _more_complete_text(node.name, candidate.name)
                    bucket = "high"
                elif score is not None and score >= low:
                    result = judge(node, candidate)
                    decision = result.decision
                    canonical_name = result.canonical_name
                    reason = result.reason
                    bucket = "llm"
                else:
                    reason = f"cosine {float(score or 0):.6f} < LOW {low:.6f}"

                score_text = "" if score is None else f" score={score:.6f}"
                prefix = "DRY-RUN " if dry_run else ""
                stats.judgments.append(
                    f"{prefix}{bucket.upper()} {pair[0]} <-> {pair[1]}: "
                    f"{decision}{score_text} ({reason})"
                )
                if decision != "same":
                    stats.skipped += 1
                    continue

                if dry_run:
                    if bucket == "exact":
                        stats.exact_merged += 1
                    elif bucket == "high":
                        stats.high_merged += 1
                    else:
                        stats.llm_merged += 1
                    continue
                try:
                    expected_fingerprints = {
                        node.graph_node_no: fingerprints[node.graph_node_no],
                        candidate.graph_node_no: fingerprints[
                            candidate.graph_node_no
                        ],
                    }
                    merge(
                        node.graph_node_no,
                        candidate.graph_node_no,
                        canonical_name=canonical_name,
                        reason=reason,
                        score=score,
                        expected_roots=pair,
                        expected_fingerprints=expected_fingerprints,
                    )
                    if bucket == "exact":
                        stats.exact_merged += 1
                    elif bucket == "high":
                        stats.high_merged += 1
                    else:
                        stats.llm_merged += 1
                    merged_this_round = True
                    break  # reload the actual survivor and current candidates
                except ValueError as exc:
                    if "candidates must be regenerated" in str(exc):
                        processed_pairs.discard(pair)
                        retry_with_fresh_candidates = True
                        stats.judgments.append(
                            f"RETRY {pair[0]} <-> {pair[1]}: {exc}"
                        )
                        break
                    stats.failed += 1
                    stats.judgments.append(f"merge skipped for {pair}: {exc}")

            if retry_with_fresh_candidates:
                continue
            if dry_run or not merged_this_round:
                break
    return stats
