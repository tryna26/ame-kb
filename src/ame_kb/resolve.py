"""V5 cross-document entity fusion (resolve).

Pipeline per node: vector/full-text KNN candidates -> LLM same/related/different
judge -> merge duplicates into a canonical survivor. Merges are:
  - field-union (properties + ref) with the survivor winning conflicts,
  - edge remap: every edge touching the loser is re-pointed at the winner,
  - alias recorded (kg_entity_alias) so the old name still resolves,
  - fully snapshotted (kg_merge_log) so a merge can be rolled back.

Borrows: general_recall dual-channel KNN candidate retrieval + canonical
node_no edge remap; Graphiti's "is_duplicate -> return the most complete name"
judge; the project's own extract.py LLM call pattern.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .embed import embed_query, embedding_available
from .extract import _extract_json, call_llm
from .models import EntityAlias, GraphEdge, GraphNode, MergeLog
from .searchbackend import SearchFilters, get_index
from .searchindex import NODE, aliases_for, build_searchable_text
from .store import edge_no, merge_props, merge_ref_maps

logger = logging.getLogger(__name__)


@dataclass
class ResolveStats:
    scanned: int = 0
    merged: int = 0
    skipped: int = 0
    judgments: List[str] = field(default_factory=list)


def _load_prompt() -> str:
    return (
        resources.files("ame_kb.prompts")
        .joinpath("resolve_v5.txt")
        .read_text(encoding="utf-8")
    )


def _fill_prompt(template: str, mapping: Dict[str, str]) -> str:
    """Single-pass safe substitution of {{placeholder}} tokens.

    Uses re.sub with a replacement function so already-substituted content is
    never re-scanned for further placeholders (a value containing a literal
    ``{{...}}`` cannot corrupt the output). Unknown placeholders are left as-is.
    """
    return re.sub(
        r"\{\{(\w+)\}\}",
        lambda m: mapping.get(m.group(1), m.group(0)),
        template,
    )


def judge(a: Dict, b: Dict) -> Dict:
    """Ask the LLM whether two entities are the same. Returns
    {"verdict": same|related|different, "canonical_name": str, "reason": str}.

    Inputs are dicts with name/type/description/properties. Any LLM/parse
    failure degrades to a conservative 'different'.
    """
    mapping = {
        "name_a": str(a.get("name", "")),
        "type_a": str(a.get("type", "")),
        "desc_a": str(a.get("description") or ""),
        "props_a": json.dumps(a.get("properties") or {}, ensure_ascii=False),
        "name_b": str(b.get("name", "")),
        "type_b": str(b.get("type", "")),
        "desc_b": str(b.get("description") or ""),
        "props_b": json.dumps(b.get("properties") or {}, ensure_ascii=False),
    }
    prompt = _fill_prompt(_load_prompt(), mapping)
    try:
        payload = _extract_json(call_llm(prompt))
    except Exception:  # noqa: BLE001 - bad JSON / no LLM -> don't merge
        return {"verdict": "different", "canonical_name": "", "reason": "judge failed"}
    verdict = str(payload.get("verdict", "different")).lower()
    if verdict not in ("same", "related", "different"):
        verdict = "different"
    return {
        "verdict": verdict,
        "canonical_name": payload.get("canonical_name", "") or "",
        "reason": payload.get("reason", "") or "",
    }


def find_candidates(
    session: Session, node: GraphNode, limit: Optional[int] = None
) -> List[GraphNode]:
    """Return up to `limit` other live nodes that are plausible duplicates of
    `node`, via the hybrid index (vector when embeddings are on, else text)."""
    settings = get_settings()
    limit = limit or settings.resolve_candidate_topk
    query = build_searchable_text(node.name, node.description, node.properties or {})
    query_embedding = None
    if embedding_available():
        try:
            query_embedding = embed_query(query)
        except Exception:  # noqa: BLE001 - fall back to text-only channel
            query_embedding = None
    warnings: List[str] = []
    nos = get_index().search(
        query,
        filters=SearchFilters(
            graph_no=settings.graph_no,
            graph_version=settings.graph_version,
            object_type=NODE,
        ),
        limit=limit + 1,  # +1 because the node itself will be in the results
        min_score_text=settings.min_score_text,
        min_score_embedding=settings.resolve_min_score_embedding,
        query_embedding=query_embedding,
        warnings=warnings,
    )
    candidate_nos = [n for n in nos if n != node.graph_node_no][:limit]
    if not candidate_nos:
        return []
    rows = (
        session.execute(
            select(GraphNode).where(
                GraphNode.graph_no == settings.graph_no,
                GraphNode.graph_version == settings.graph_version,
                GraphNode.graph_node_no.in_(candidate_nos),
                GraphNode.deleted == 0,
            )
        )
        .scalars()
        .all()
    )
    order = {no: i for i, no in enumerate(candidate_nos)}
    rows.sort(key=lambda r: order.get(r.graph_node_no, 1 << 30))
    return rows


def _node_snapshot(node: GraphNode) -> Dict:
    return {
        "graph_node_no": node.graph_node_no,
        "name": node.name,
        "type": node.type,
        "description": node.description,
        "properties": node.properties or {},
        "ref": node.ref or {},
        "deleted": node.deleted,
    }


def _reindex_node(session: Session, node: GraphNode) -> None:
    alias_map = aliases_for(session, [node.graph_node_no])
    from .searchindex import upsert_search_index

    upsert_search_index(
        session,
        [
            {
                "object_type": NODE,
                "object_no": node.graph_node_no,
                "searchable_text": build_searchable_text(
                    node.name,
                    node.description,
                    node.properties or {},
                    alias_map.get(node.graph_node_no),
                ),
            }
        ],
    )


def _remap_edges(session: Session, settings, winner_no: str, loser_no: str) -> List[Dict]:
    """Re-point every edge touching loser_no at winner_no. Returns a per-edge
    snapshot list for rollback. Handles self-loops and edge_no collisions."""
    from .searchindex import EDGE

    edges = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.graph_no == settings.graph_no,
                GraphEdge.graph_version == settings.graph_version,
                GraphEdge.deleted == 0,
                (GraphEdge.source_node_no == loser_no)
                | (GraphEdge.target_node_no == loser_no),
            )
        )
        .scalars()
        .all()
    )
    index = get_index()
    edge_snaps: List[Dict] = []
    for e in edges:
        old_src, old_dst, old_no = e.source_node_no, e.target_node_no, e.graph_edge_no
        new_src = winner_no if old_src == loser_no else old_src
        new_dst = winner_no if old_dst == loser_no else old_dst
        base = {
            "id": e.id,
            "old_source": old_src,
            "old_target": old_dst,
            "old_edge_no": old_no,
        }
        # A remap that collapses both endpoints onto the winner is a self-loop;
        # drop it (a node is not related to itself).
        if new_src == new_dst:
            e.deleted = 1
            index.delete_objects(EDGE, [old_no], session=session)
            edge_snaps.append({**base, "action": "deleted_selfloop"})
            continue
        new_no = edge_no(new_src, e.name, new_dst)
        survivor = session.execute(
            select(GraphEdge).where(
                GraphEdge.graph_no == settings.graph_no,
                GraphEdge.graph_version == settings.graph_version,
                GraphEdge.graph_edge_no == new_no,
                GraphEdge.deleted == 0,
                GraphEdge.id != e.id,
            )
        ).scalar_one_or_none()
        if survivor is not None:
            # An identical edge already exists post-remap: fold this one in and
            # drop it. Snapshot the survivor's pre-merge state to undo the fold.
            edge_snaps.append(
                {
                    **base,
                    "action": "deleted_dup",
                    "survivor_id": survivor.id,
                    "survivor_props_before": dict(survivor.properties or {}),
                    "survivor_ref_before": dict(survivor.ref or {}),
                }
            )
            survivor.properties = merge_props(
                survivor.properties or {}, e.properties or {}
            )
            survivor.ref = merge_ref_maps(survivor.ref or {}, e.ref or {})
            e.deleted = 1
            index.delete_objects(EDGE, [old_no], session=session)
            _reindex_edge(session, survivor)
        else:
            e.source_node_no = new_src
            e.target_node_no = new_dst
            e.graph_edge_no = new_no
            index.delete_objects(EDGE, [old_no], session=session)
            _reindex_edge(session, e)
            edge_snaps.append({**base, "action": "remapped"})
    return edge_snaps


def _reindex_edge(session: Session, edge: GraphEdge) -> None:
    from .searchindex import EDGE, upsert_search_index

    upsert_search_index(
        session,
        [
            {
                "object_type": EDGE,
                "object_no": edge.graph_edge_no,
                "searchable_text": build_searchable_text(
                    edge.name, edge.description, edge.properties or {}
                ),
            }
        ],
    )


def merge(winner_no: str, loser_no: str, *, canonical_name: str = "") -> str:
    """Merge loser into winner (field-union + edge remap + alias + audit).

    Returns the merge_id. Raises ValueError if either node is missing.
    """
    settings = get_settings()

    with session_scope() as session:
        winner = _load_node(session, settings, winner_no)
        loser = _load_node(session, settings, loser_no)
        if winner is None or loser is None:
            raise ValueError(f"merge needs two live nodes: {winner_no}, {loser_no}")

        snapshot: Dict = {
            "winner": _node_snapshot(winner),
            "loser": _node_snapshot(loser),
            "aliases_added": [],
            "winner_name_changed": False,
            "edges": [],
        }

        # Field union: survivor wins conflicts, loser fills gaps.
        winner.properties = merge_props(winner.properties or {}, loser.properties or {})
        winner.ref = merge_ref_maps(winner.ref or {}, loser.ref or {})
        if not winner.description and loser.description:
            winner.description = loser.description
        elif loser.description and len(loser.description) > len(winner.description or ""):
            winner.description = loser.description

        # Record the loser's name as an alias of the winner.
        aliases_added = _add_alias(session, settings, winner_no, loser.name)

        # If the LLM proposed a more complete canonical name, adopt it and keep
        # the winner's old name as an alias too.
        if canonical_name and canonical_name != winner.name:
            aliases_added += _add_alias(session, settings, winner_no, winner.name)
            winner.name = canonical_name
            snapshot["winner_name_changed"] = True
        snapshot["aliases_added"] = aliases_added

        # Remap edges, then soft-delete the loser and drop it from the index.
        snapshot["edges"] = _remap_edges(session, settings, winner_no, loser_no)
        loser.deleted = 1
        get_index().delete_objects(NODE, [loser_no], session=session)

        _reindex_node(session, winner)

        merge_id = uuid.uuid4().hex
        session.add(
            MergeLog(
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
                merge_id=merge_id,
                winner_node_no=winner_no,
                loser_node_no=loser_no,
                snapshot=snapshot,
                status="MERGED",
            )
        )
    return merge_id


def rollback(merge_id: str) -> None:
    """Undo a merge from its snapshot: restore both nodes, un-remap edges, drop
    the aliases we added, and reindex. Raises ValueError if not found/undone."""
    settings = get_settings()
    with session_scope() as session:
        log = session.execute(
            select(MergeLog).where(
                MergeLog.graph_no == settings.graph_no,
                MergeLog.graph_version == settings.graph_version,
                MergeLog.merge_id == merge_id,
            )
        ).scalar_one_or_none()
        if log is None:
            raise ValueError(f"no merge with id {merge_id}")
        if log.status == "ROLLED_BACK":
            raise ValueError(f"merge {merge_id} already rolled back")
        snap = log.snapshot or {}

        winner = _load_node(session, settings, log.winner_node_no)
        loser = _load_node(session, settings, log.loser_node_no, include_deleted=True)
        if winner is None or loser is None:
            raise ValueError("cannot rollback: winner/loser node missing")

        # Restore both node rows to their pre-merge state.
        w = snap.get("winner", {})
        winner.name = w.get("name", winner.name)
        winner.description = w.get("description")
        winner.properties = w.get("properties") or {}
        winner.ref = w.get("ref") or {}
        lo = snap.get("loser", {})
        loser.name = lo.get("name", loser.name)
        loser.description = lo.get("description")
        loser.properties = lo.get("properties") or {}
        loser.ref = lo.get("ref") or {}
        loser.deleted = 0

        # Reverse edge changes (order does not matter; each keyed by DB id).
        _rollback_edges(session, settings, snap.get("edges", []))

        # Drop the aliases this merge added.
        for alias in snap.get("aliases_added", []):
            session.execute(
                EntityAlias.__table__.delete().where(
                    EntityAlias.graph_no == settings.graph_no,
                    EntityAlias.graph_version == settings.graph_version,
                    EntityAlias.canonical_node_no == log.winner_node_no,
                    EntityAlias.alias == alias,
                )
            )

        _reindex_node(session, winner)
        _reindex_node(session, loser)
        log.status = "ROLLED_BACK"


def _rollback_edges(session: Session, settings, edge_snaps: List[Dict]) -> None:
    from .searchindex import EDGE

    index = get_index()
    for snap in edge_snaps:
        edge = session.get(GraphEdge, snap["id"])
        if edge is None:
            continue
        action = snap.get("action")
        new_no = edge.graph_edge_no
        edge.source_node_no = snap["old_source"]
        edge.target_node_no = snap["old_target"]
        edge.graph_edge_no = snap["old_edge_no"]
        if action in ("deleted_selfloop", "deleted_dup"):
            edge.deleted = 0
        if action == "deleted_dup":
            survivor = session.get(GraphEdge, snap["survivor_id"])
            if survivor is not None:
                survivor.properties = snap.get("survivor_props_before") or {}
                survivor.ref = snap.get("survivor_ref_before") or {}
                _reindex_edge(session, survivor)
        else:
            # remapped: the post-merge edge_no row is now stale.
            index.delete_objects(EDGE, [new_no], session=session)
        _reindex_edge(session, edge)


def _add_alias(session: Session, settings, canonical_no: str, alias: str) -> List[str]:
    """Insert an alias row if absent. Returns [alias] if inserted, else []."""
    if not alias:
        return []
    existing = session.execute(
        select(EntityAlias.canonical_node_no).where(
            EntityAlias.graph_no == settings.graph_no,
            EntityAlias.graph_version == settings.graph_version,
            EntityAlias.alias == alias,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing != canonical_no:
            # The alias is already registered to a DIFFERENT canonical node.
            # Respect the unique constraint (do not insert a duplicate) but warn
            # so the mis-mapping is not silently dropped.
            logger.warning(
                "alias collision: alias=%r already maps to canonical=%r; "
                "refusing to remap to canonical=%r",
                alias,
                existing,
                canonical_no,
            )
        return []
    session.add(
        EntityAlias(
            graph_no=settings.graph_no,
            graph_version=settings.graph_version,
            canonical_node_no=canonical_no,
            alias=alias,
            source="merge",
        )
    )
    return [alias]


def _load_node(session, settings, node_no_: str, include_deleted: bool = False):
    conds = [
        GraphNode.graph_no == settings.graph_no,
        GraphNode.graph_version == settings.graph_version,
        GraphNode.graph_node_no == node_no_,
    ]
    if not include_deleted:
        conds.append(GraphNode.deleted == 0)
    return session.execute(select(GraphNode).where(*conds)).scalar_one_or_none()


def alias_of(canonical_name_or_no: str) -> List[str]:
    """List aliases whose canonical node matches the given node_no or name."""
    settings = get_settings()
    with session_scope() as session:
        canonical_no = canonical_name_or_no
        node = _load_node(session, settings, canonical_name_or_no)
        if node is None:
            node = session.execute(
                select(GraphNode).where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.name == canonical_name_or_no,
                    GraphNode.deleted == 0,
                )
            ).scalar_one_or_none()
            if node is not None:
                canonical_no = node.graph_node_no
        rows = (
            session.execute(
                select(EntityAlias.alias).where(
                    EntityAlias.graph_no == settings.graph_no,
                    EntityAlias.graph_version == settings.graph_version,
                    EntityAlias.canonical_node_no == canonical_no,
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


def resolve_all(
    type_filter: Optional[str] = None, dry_run: bool = False, limit: Optional[int] = None
) -> ResolveStats:
    """Scan live nodes; for each, find candidates, judge, and merge duplicates.

    Nodes judged the same collapse via union-find into a single deterministic
    survivor (the earliest-scanned member of the set), so every loser merges
    directly into that one winner. This avoids chained merges where a node that
    already won a merge is later used as a loser (which would strand aliases and
    edges). Each unordered pair is judged at most once per run.
    """
    settings = get_settings()
    stats = ResolveStats()
    with session_scope() as session:
        conds = [
            GraphNode.graph_no == settings.graph_no,
            GraphNode.graph_version == settings.graph_version,
            GraphNode.deleted == 0,
        ]
        if type_filter:
            conds.append(GraphNode.type == type_filter)
        node_nos = (
            session.execute(
                select(GraphNode.graph_node_no).where(*conds).order_by(GraphNode.id)
            )
            .scalars()
            .all()
        )

    # Union-find over node_nos. The set representative is always the member that
    # appears earliest in scan order (smallest GraphNode.id), so find(x) yields
    # the deterministic final winner for x's set.
    order_index = {no: i for i, no in enumerate(node_nos)}
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Keep the earliest-in-scan-order member as the representative.
        if order_index.get(ra, 1 << 30) <= order_index.get(rb, 1 << 30):
            parent[rb] = ra
        else:
            parent[ra] = rb

    judged_pairs: set = set()  # frozenset({no_a, no_b}) already sent to judge()
    same_pairs: List[tuple] = []  # (no_a, no_b, canonical_name) judged "same"

    for nno in node_nos:
        # Skip nodes already absorbed as a loser (they have an earlier root).
        # Do NOT skip a node merely because it is a winner/representative.
        if find(nno) != nno:
            continue
        stats.scanned += 1
        with session_scope() as session:
            node = _load_node(session, settings, nno)
            if node is None:
                continue
            candidates = find_candidates(session, node, limit)
            node_view = _node_snapshot(node)
            cand_views = [
                (c.graph_node_no, _node_snapshot(c)) for c in candidates
            ]
        for cand_no, cand_view in cand_views:
            # Skip candidates already absorbed as a loser, already in the same
            # set, or unordered pairs we have already judged this run.
            if find(cand_no) != cand_no:
                continue
            if find(cand_no) == find(nno):
                continue
            pair_key = frozenset((nno, cand_no))
            if pair_key in judged_pairs:
                continue
            judged_pairs.add(pair_key)
            verdict = judge(node_view, cand_view)
            if verdict["verdict"] == "same":
                stats.judgments.append(
                    f"SAME {node_view['name']} == {cand_view['name']} "
                    f"({verdict['reason']})"
                )
                union(nno, cand_no)
                same_pairs.append((nno, cand_no, verdict["canonical_name"]))
            else:
                stats.skipped += 1

    # Pick the best canonical_name per surviving set (first non-empty, in the
    # order pairs were judged -> deterministic).
    canonical_for_root: Dict[str, str] = {}
    for a, _b, cname in same_pairs:
        root = find(a)
        if cname and not canonical_for_root.get(root):
            canonical_for_root[root] = cname

    # Every non-winner member of a set merges directly into the one survivor.
    involved = {n for pair in same_pairs for n in pair[:2]}
    members_by_root: Dict[str, List[str]] = {}
    for no in sorted(involved, key=lambda n: order_index.get(n, 1 << 30)):
        members_by_root.setdefault(find(no), []).append(no)

    pending: List[tuple] = []  # (winner_no, loser_no, canonical_name)
    for root, members in members_by_root.items():
        cname = canonical_for_root.get(root, "")
        for member in members:
            if member == root:
                continue  # the winner is never used as a loser
            pending.append((root, member, cname))

    if dry_run:
        return stats
    for winner_no, loser_no, canonical in pending:
        try:
            merge(winner_no, loser_no, canonical_name=canonical)
            stats.merged += 1
        except ValueError as exc:
            stats.judgments.append(f"merge skipped: {exc}")
    return stats
