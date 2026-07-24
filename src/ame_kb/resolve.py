"""Cross-document entity fusion (resolve).

Per seed node: vector/full-text KNN candidates -> one **batch** LLM call that
clusters synonymous names -> merge each cluster into a canonical survivor.
Batch clustering replaces the old O(n*candidates) pairwise judge: one LLM call
per seed groups all its candidates at once, which is far cheaper in tokens.
Merges are:
  - field-union (properties + ref) with the survivor winning conflicts,
  - edge remap: every edge touching the loser is re-pointed at the winner,
  - alias recorded (kg_entity_alias) so the old name still resolves,
  - fully snapshotted (kg_merge_log) so a merge can be rolled back.

An optional low-support pruning pass (opt-in) soft-deletes long-tail noise
nodes -- those seen in fewer than `resolve_prune_min_support` docs and touched
by no live edge -- also snapshotted for rollback.

Borrows: general_recall dual-channel KNN candidate retrieval + canonical
node_no edge remap; oceanai_site's batch synonym clustering + low-support
pruning; Graphiti's "return the most complete name" canonicalization.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .embed import embed_query, embedding_available
from .extract import _extract_json, call_llm
from .models import DomainEntity, EntityAlias, GraphEdge, GraphNode, MergeLog
from .searchbackend import SearchFilters, get_index
from .searchindex import NODE, aliases_for, build_searchable_text
from .store import edge_no, load_domain_map, merge_props, merge_ref_maps

logger = logging.getLogger(__name__)


@dataclass
class ResolveStats:
    scanned: int = 0
    merged: int = 0
    skipped: int = 0
    pruned: int = 0
    judgments: List[str] = field(default_factory=list)


def _load_prompt(name: str) -> str:
    return (
        resources.files("ame_kb.prompts")
        .joinpath(name)
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


def cluster(entity_type: str, entities: List[Dict]) -> List[Dict]:
    """Group synonymous names among same-type candidates in one LLM call.

    `entities` are dicts with name/description/properties. Returns a list of
    {"canonical": str, "names": [str, ...]} clusters (each with >= 2 names).
    Any LLM/parse failure degrades to an empty list (merge nothing).
    """
    if len(entities) < 2:
        return []
    payload_entities = [
        {
            "name": str(e.get("name", "")),
            "description": str(e.get("description") or ""),
            "properties": e.get("properties") or {},
        }
        for e in entities
    ]
    mapping = {
        "type": str(entity_type or ""),
        "entities": json.dumps(payload_entities, ensure_ascii=False),
    }
    prompt = _fill_prompt(_load_prompt("resolve_cluster.txt"), mapping)
    try:
        parsed = _extract_json(call_llm(prompt))
    except Exception:  # noqa: BLE001 - bad JSON / no LLM -> merge nothing
        return []
    valid_names = {e["name"] for e in payload_entities if e["name"]}
    clusters: List[Dict] = []
    for raw in parsed.get("clusters") or []:
        # Only keep names the model was actually given (no invented names).
        names = [n for n in (raw.get("names") or []) if n in valid_names]
        names = list(dict.fromkeys(names))  # dedup, keep order
        if len(names) < 2:
            continue
        canonical = raw.get("canonical") or ""
        if canonical not in valid_names:
            canonical = ""
        clusters.append({"canonical": canonical, "names": names})
    return clusters


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


def _node_snapshot(
    node: GraphNode, domain: Optional[tuple] = None
) -> Dict:
    dtype, dspec = domain if domain else ("", None)
    return {
        "graph_node_no": node.graph_node_no,
        "name": node.name,
        "type": dtype,
        "entity_spec": dspec,
        "description": node.description,
        "properties": node.properties or {},
        "ref": node.ref or {},
        "deleted": node.deleted,
    }


def _domain_of(session: Session, settings, node_no_: str) -> tuple:
    """Return (type, entity_spec) for a node from the domain layer, or ('', None)."""
    dm = load_domain_map(
        session, settings.graph_no, settings.graph_version, [node_no_]
    )
    return dm.get(node_no_, ("", None))


def _set_domain_deleted(session: Session, settings, node_no_: str, deleted: int) -> None:
    session.execute(
        DomainEntity.__table__.update()
        .where(
            DomainEntity.graph_no == settings.graph_no,
            DomainEntity.graph_version == settings.graph_version,
            DomainEntity.graph_node_no == node_no_,
        )
        .values(deleted=deleted)
    )


def _soft_delete_domain(session: Session, settings, node_no_: str) -> None:
    _set_domain_deleted(session, settings, node_no_, 1)


def _restore_domain(session: Session, settings, node_no_: str) -> None:
    _set_domain_deleted(session, settings, node_no_, 0)


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
            "winner": _node_snapshot(
                winner, _domain_of(session, settings, winner_no)
            ),
            "loser": _node_snapshot(
                loser, _domain_of(session, settings, loser_no)
            ),
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
        _soft_delete_domain(session, settings, loser_no)
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
    """Undo a merge or prune from its snapshot. For a merge: restore both nodes,
    un-remap edges, drop the aliases we added, and reindex. For a prune: restore
    the single soft-deleted node. Raises ValueError if not found/already undone."""
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

        # A prune snapshot only soft-deleted a single node (no winner/edges).
        if log.status == "PRUNED" or snap.get("kind") == "prune":
            loser = _load_node(
                session, settings, log.loser_node_no, include_deleted=True
            )
            if loser is None:
                raise ValueError("cannot rollback: pruned node missing")
            lo = snap.get("loser", {})
            loser.name = lo.get("name", loser.name)
            loser.description = lo.get("description")
            loser.properties = lo.get("properties") or {}
            loser.ref = lo.get("ref") or {}
            loser.deleted = 0
            _restore_domain(session, settings, log.loser_node_no)
            _reindex_node(session, loser)
            log.status = "ROLLED_BACK"
            return

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
        _restore_domain(session, settings, log.loser_node_no)

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


def _live_node_nos(session, settings, type_filter: Optional[str]) -> List[str]:
    """Live node business keys in scan order, optionally filtered by ontology type."""
    conds = [
        GraphNode.graph_no == settings.graph_no,
        GraphNode.graph_version == settings.graph_version,
        GraphNode.deleted == 0,
    ]
    if type_filter:
        # The ontology class lives in the domain layer; filter via a join on the
        # shared graph_node_no.
        stmt = (
            select(GraphNode.graph_node_no)
            .join(
                DomainEntity,
                and_(
                    DomainEntity.graph_no == GraphNode.graph_no,
                    DomainEntity.graph_version == GraphNode.graph_version,
                    DomainEntity.graph_node_no == GraphNode.graph_node_no,
                ),
            )
            .where(*conds, DomainEntity.type == type_filter)
            .order_by(GraphNode.id)
        )
    else:
        stmt = select(GraphNode.graph_node_no).where(*conds).order_by(GraphNode.id)
    return list(session.execute(stmt).scalars().all())


def resolve_all(
    type_filter: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    prune: Optional[bool] = None,
) -> ResolveStats:
    """Scan live nodes; for each, KNN-recall same-type candidates, batch-cluster
    synonyms in one LLM call, and merge each cluster into a canonical survivor.

    Nodes clustered together collapse via union-find into a single deterministic
    survivor (the earliest-scanned member of the set), so every loser merges
    directly into that one winner. This avoids chained merges where a node that
    already won a merge is later used as a loser (which would strand aliases and
    edges).

    When `prune` (or the RESOLVE_PRUNE_ENABLED default) is on, a final pass
    soft-deletes low-support long-tail noise nodes (see `prune_low_support`).
    """
    settings = get_settings()
    stats = ResolveStats()
    with session_scope() as session:
        node_nos = _live_node_nos(session, settings, type_filter)

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

    canonical_for_root: Dict[str, str] = {}
    involved: set = set()

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
            cand_nos = [c.graph_node_no for c in candidates]
            domain_map = load_domain_map(
                session,
                settings.graph_no,
                settings.graph_version,
                [nno] + cand_nos,
            )
            seed_view = _node_snapshot(node, domain_map.get(nno))
            cand_views = [
                (c.graph_node_no, _node_snapshot(c, domain_map.get(c.graph_node_no)))
                for c in candidates
            ]

        seed_type = seed_view.get("type") or ""
        # Only cluster within the same ontology type, and drop candidates already
        # absorbed as a loser or already in the seed's set.
        pool = [(nno, seed_view)]
        for cand_no, cand_view in cand_views:
            if find(cand_no) != cand_no or find(cand_no) == find(nno):
                continue
            if (cand_view.get("type") or "") != seed_type:
                continue
            pool.append((cand_no, cand_view))
        if len(pool) < 2:
            continue

        name_to_no: Dict[str, str] = {}
        for pno, pview in pool:
            name_to_no.setdefault(pview["name"], pno)
        clusters = cluster(seed_type, [pv for _pno, pv in pool])
        for grp in clusters:
            members = [name_to_no[n] for n in grp["names"] if n in name_to_no]
            members = [m for m in members if find(m) == m]  # skip absorbed
            members = list(dict.fromkeys(members))
            if len(members) < 2:
                stats.skipped += 1
                continue
            base = members[0]
            for m in members[1:]:
                union(base, m)
            root = find(base)
            cname = grp.get("canonical") or ""
            if cname and not canonical_for_root.get(root):
                canonical_for_root[root] = cname
            involved.update(members)
            stats.judgments.append(
                f"CLUSTER {cname or seed_view['name']} <= "
                f"[{', '.join(grp['names'])}]"
            )

    # Every non-winner member of a set merges directly into the one survivor.
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

    do_prune = prune if prune is not None else settings.resolve_prune_enabled

    if dry_run:
        if do_prune:
            pstats = prune_low_support(type_filter=type_filter, dry_run=True)
            stats.pruned = pstats.pruned
            stats.judgments.extend(pstats.judgments)
        return stats

    for winner_no, loser_no, canonical in pending:
        try:
            merge(winner_no, loser_no, canonical_name=canonical)
            stats.merged += 1
        except ValueError as exc:
            stats.judgments.append(f"merge skipped: {exc}")

    if do_prune:
        pstats = prune_low_support(type_filter=type_filter)
        stats.pruned = pstats.pruned
        stats.judgments.extend(pstats.judgments)
    return stats


def _edge_referenced_nodes(session, settings) -> set:
    """Set of node_nos touched by at least one live edge (both endpoints)."""
    rows = session.execute(
        select(GraphEdge.source_node_no, GraphEdge.target_node_no).where(
            GraphEdge.graph_no == settings.graph_no,
            GraphEdge.graph_version == settings.graph_version,
            GraphEdge.deleted == 0,
        )
    ).all()
    referenced: set = set()
    for src, dst in rows:
        referenced.add(src)
        referenced.add(dst)
    return referenced


def prune_low_support(
    type_filter: Optional[str] = None,
    min_support: Optional[int] = None,
    dry_run: bool = False,
) -> ResolveStats:
    """Soft-delete long-tail noise nodes: those seen in fewer than `min_support`
    distinct docs (``len(ref)``) and touched by no live edge. Each drop is
    snapshotted to kg_merge_log (status PRUNED) so it can be rolled back.

    Nodes referenced by any live edge are always protected -- pruning them would
    orphan real relations.
    """
    settings = get_settings()
    min_support = (
        min_support if min_support is not None else settings.resolve_prune_min_support
    )
    stats = ResolveStats()
    with session_scope() as session:
        node_nos = _live_node_nos(session, settings, type_filter)
        referenced = _edge_referenced_nodes(session, settings)
        for nno in node_nos:
            node = _load_node(session, settings, nno)
            if node is None or nno in referenced:
                continue
            if len(node.ref or {}) >= min_support:
                continue
            stats.judgments.append(
                f"PRUNE {node.name} (support={len(node.ref or {})})"
            )
            stats.pruned += 1
            if dry_run:
                continue
            snapshot = {
                "loser": _node_snapshot(
                    node, _domain_of(session, settings, nno)
                ),
                "kind": "prune",
            }
            node.deleted = 1
            _soft_delete_domain(session, settings, nno)
            get_index().delete_objects(NODE, [nno], session=session)
            session.add(
                MergeLog(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    merge_id=uuid.uuid4().hex,
                    winner_node_no="",
                    loser_node_no=nno,
                    snapshot=snapshot,
                    status="PRUNED",
                )
            )
    return stats
