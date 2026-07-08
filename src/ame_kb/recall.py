"""V3 hybrid recall, ported from general_recall node_retriever.go (:261-387).

Ten steps:
 1. Hybrid-recall NODEs: FULLTEXT(ngram) + vector cosine, each min-score
    filtered, fused with RRF. A failing channel only warns and continues.
 2. Hybrid-recall EDGEs the same way.
 3. Candidate pool = node hits ∪ edge.source_node_no ∪ edge.target_node_no
    (dedup, order-preserving; both endpoints enter the pool).
 4. Rerank the pool against the original query, restricted to the pool set.
 5. Take topK seeds.
 6. MySQL one-hop edges of the seeds (source/target IN (...)).
 7. Batch-fetch neighbor node details (no N+1).
 8. Rerank neighbors against the original query (neighborTopK, no type filter).
 9. Resolve Ref {doc_no: [lines/ranges]} to original lines from kg_doc_line
    (expand ranges, merge gaps ≤ 3, optional ±window).
10. Return seeds + neighbors + evidence lines.

Per-channel text×vector fusion happens here (ByteRAG does rank=rrf internally,
byterag_store.go:42). Application-layer RRF (rrf.py) is only for multi-query /
multi-list fusion and stays separate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .embed import embed_query, embedding_available
from .models import DocLine, GraphEdge, GraphNode, SearchIndex
from .rrf import rrf_merge

NODE = "NODE"
EDGE = "EDGE"
_ADJACENT_GAP = 3  # merge line ranges whose gap is <= this many lines


@dataclass
class NodeResult:
    graph_node_no: str
    name: str
    type: str
    description: Optional[str]
    properties: dict
    ref: dict


@dataclass
class EdgeResult:
    graph_edge_no: str
    source_node_no: str
    target_node_no: str
    label: str
    description: Optional[str]
    ref: dict


@dataclass
class EvidenceLine:
    doc_no: str
    line_no: int
    content: str


@dataclass
class RecallResult:
    query: str
    seeds: List[NodeResult] = field(default_factory=list)
    neighbors: List[NodeResult] = field(default_factory=list)
    edges: List[EdgeResult] = field(default_factory=list)
    evidence: List[EvidenceLine] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0 if either is empty/zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def build_candidate_pool(
    node_hits: Sequence[str],
    edge_hits: Sequence[str],
    edge_endpoints: Dict[str, Tuple[str, str]],
) -> List[str]:
    """Step 3 candidate pool, pure/testable.

    Pool = node hits ∪ edge source ∪ edge target, deduped and order-preserving:
    node hits first (in order), then each hit edge's two endpoints. Edges not in
    edge_endpoints are skipped. Mirrors node_retriever.go:343-362.
    """
    pool: List[str] = []
    seen = set()

    def _add(no: str) -> None:
        if no and no not in seen:
            seen.add(no)
            pool.append(no)

    for no in node_hits:
        _add(no)
    for eno in edge_hits:
        endpoints = edge_endpoints.get(eno)
        if endpoints is None:
            continue
        src, dst = endpoints
        _add(src)
        _add(dst)
    return pool


def _fulltext_channel(
    session: Session,
    query: str,
    object_type: str,
    min_score: float,
    limit: int,
    restrict: Optional[Sequence[str]] = None,
) -> List[str]:
    settings = get_settings()
    sql = (
        "SELECT object_no, "
        "MATCH(searchable_text) AGAINST(:q IN NATURAL LANGUAGE MODE) AS score "
        "FROM kg_search_index "
        "WHERE graph_no = :g AND graph_version = :v AND object_type = :t "
        "AND MATCH(searchable_text) AGAINST(:q IN NATURAL LANGUAGE MODE) > :min "
    )
    params = {
        "q": query,
        "g": settings.graph_no,
        "v": settings.graph_version,
        "t": object_type,
        "min": min_score,
        "lim": limit,
    }
    if restrict:
        sql += "AND object_no IN :nos "
        params["nos"] = list(restrict)
    sql += "ORDER BY score DESC LIMIT :lim"
    stmt = text(sql)
    if restrict:
        stmt = stmt.bindparams(bindparam("nos", expanding=True))
    rows = session.execute(stmt, params).all()
    return [r[0] for r in rows]


def _vector_channel(
    session: Session,
    query_embedding: Sequence[float],
    object_type: str,
    min_score: float,
    limit: int,
    restrict: Optional[Sequence[str]] = None,
) -> List[str]:
    settings = get_settings()
    conds = [
        SearchIndex.graph_no == settings.graph_no,
        SearchIndex.graph_version == settings.graph_version,
        SearchIndex.object_type == object_type,
        SearchIndex.embedding.is_not(None),
    ]
    if restrict:
        conds.append(SearchIndex.object_no.in_(list(restrict)))
    rows = session.execute(
        select(SearchIndex.object_no, SearchIndex.embedding).where(*conds)
    ).all()
    scored: List[Tuple[str, float]] = []
    for object_no, emb in rows:
        if not emb:
            continue
        score = cosine(query_embedding, emb)
        if score > min_score:
            scored.append((object_no, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [no for no, _ in scored[:limit]]


def _hybrid_recall(
    session: Session,
    query: str,
    object_type: str,
    limit: int,
    warnings: List[str],
    query_embedding: Optional[Sequence[float]] = None,
    restrict: Optional[Sequence[str]] = None,
) -> List[str]:
    """One channel of text + (optional) vector, fused with RRF. A failing
    channel only warns and continues (node_retriever.go:332-341)."""
    settings = get_settings()
    channels: List[List[str]] = []

    try:
        text_hits = _fulltext_channel(
            session, query, object_type, settings.min_score_text, limit, restrict
        )
        channels.append(text_hits)
    except Exception as exc:  # noqa: BLE001 - one channel failing is non-fatal
        warnings.append(f"fulltext {object_type} channel failed: {exc}")

    if query_embedding is not None:
        try:
            vec_hits = _vector_channel(
                session,
                query_embedding,
                object_type,
                settings.min_score_embedding,
                limit,
                restrict,
            )
            channels.append(vec_hits)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"vector {object_type} channel failed: {exc}")

    if not channels:
        return []
    return rrf_merge(channels)[:limit]


def _load_nodes(session: Session, node_nos: Sequence[str]) -> Dict[str, GraphNode]:
    if not node_nos:
        return {}
    settings = get_settings()
    rows = (
        session.execute(
            select(GraphNode).where(
                GraphNode.graph_no == settings.graph_no,
                GraphNode.graph_version == settings.graph_version,
                GraphNode.deleted == 0,
                GraphNode.graph_node_no.in_(list(node_nos)),
            )
        )
        .scalars()
        .all()
    )
    return {n.graph_node_no: n for n in rows}


def _load_edges(session: Session, edge_nos: Sequence[str]) -> Dict[str, GraphEdge]:
    if not edge_nos:
        return {}
    settings = get_settings()
    rows = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.graph_no == settings.graph_no,
                GraphEdge.graph_version == settings.graph_version,
                GraphEdge.deleted == 0,
                GraphEdge.graph_edge_no.in_(list(edge_nos)),
            )
        )
        .scalars()
        .all()
    )
    return {e.graph_edge_no: e for e in rows}


def _one_hop_edges(session: Session, node_nos: Sequence[str]) -> List[GraphEdge]:
    if not node_nos:
        return []
    settings = get_settings()
    node_list = list(node_nos)
    return list(
        session.execute(
            select(GraphEdge).where(
                GraphEdge.graph_no == settings.graph_no,
                GraphEdge.graph_version == settings.graph_version,
                GraphEdge.deleted == 0,
                or_(
                    GraphEdge.source_node_no.in_(node_list),
                    GraphEdge.target_node_no.in_(node_list),
                ),
            )
        )
        .scalars()
        .all()
    )


def _node_result(n: GraphNode) -> NodeResult:
    return NodeResult(
        graph_node_no=n.graph_node_no,
        name=n.name,
        type=n.type,
        description=n.description,
        properties=n.properties or {},
        ref=n.ref or {},
    )


def _edge_result(e: GraphEdge) -> EdgeResult:
    return EdgeResult(
        graph_edge_no=e.graph_edge_no,
        source_node_no=e.source_node_no,
        target_node_no=e.target_node_no,
        label=e.name,
        description=e.description,
        ref=e.ref or {},
    )


def _parse_line_range(token: str) -> List[int]:
    """Expand a ref token like "3" or "5-7" into a list of line numbers."""
    token = str(token).strip()
    if not token:
        return []
    if "-" in token:
        lo_s, hi_s = token.split("-", 1)
        try:
            lo, hi = int(lo_s), int(hi_s)
        except ValueError:
            return []
        if lo > hi:
            lo, hi = hi, lo
        return list(range(lo, hi + 1))
    try:
        return [int(token)]
    except ValueError:
        return []


def _collect_line_numbers(ref: Dict, window: int) -> Dict[str, List[int]]:
    """ref = {doc_no: [tokens]} -> {doc_no: sorted merged line numbers}.

    Expands each token, applies ±window, then merges runs whose gap <= 3.
    """
    by_doc: Dict[str, List[int]] = {}
    for doc_no, tokens in (ref or {}).items():
        lines: set = set()
        for tok in tokens or []:
            for ln in _parse_line_range(tok):
                for w in range(ln - window, ln + window + 1):
                    if w >= 1:
                        lines.add(w)
        if not lines:
            continue
        ordered = sorted(lines)
        merged: List[int] = []
        prev = None
        for ln in ordered:
            if prev is not None and ln - prev > 1 and ln - prev <= _ADJACENT_GAP:
                merged.extend(range(prev + 1, ln))  # fill small gaps
            merged.append(ln)
            prev = ln
        by_doc[doc_no] = sorted(set(merged))
    return by_doc


def _fetch_lines(session: Session, by_doc: Dict[str, List[int]]) -> List[EvidenceLine]:
    settings = get_settings()
    out: List[EvidenceLine] = []
    for doc_no, line_nos in by_doc.items():
        if not line_nos:
            continue
        rows = (
            session.execute(
                select(DocLine.line_no, DocLine.content).where(
                    DocLine.graph_no == settings.graph_no,
                    DocLine.graph_version == settings.graph_version,
                    DocLine.doc_no == doc_no,
                    DocLine.line_no.in_(line_nos),
                )
            )
            .all()
        )
        for line_no, content in sorted(rows, key=lambda r: r[0]):
            out.append(EvidenceLine(doc_no=doc_no, line_no=line_no, content=content or ""))
    return out


def recall(query: str, window: int = 0) -> RecallResult:
    settings = get_settings()
    result = RecallResult(query=query)

    query_embedding: Optional[List[float]] = None
    if embedding_available():
        try:
            query_embedding = embed_query(query)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"query embedding failed: {exc}")

    top_k = settings.recall_topk
    neighbor_k = settings.recall_neighbor_topk
    # A wider candidate limit than top_k so reranking has room to work.
    wide = max(top_k * 4, 20)

    with session_scope() as session:
        # Steps 1-2: hybrid recall of NODE and EDGE.
        node_hits = _hybrid_recall(
            session, query, NODE, wide, result.warnings, query_embedding
        )
        edge_hits = _hybrid_recall(
            session, query, EDGE, wide, result.warnings, query_embedding
        )

        # Step 3: candidate pool (dedup, order-preserving; both endpoints in).
        edges_by_no = _load_edges(session, edge_hits)
        endpoints = {
            eno: (e.source_node_no, e.target_node_no)
            for eno, e in edges_by_no.items()
        }
        pool = build_candidate_pool(node_hits, edge_hits, endpoints)

        # Step 4: rerank within the pool against the original query.
        reranked = _hybrid_recall(
            session, query, NODE, wide, result.warnings, query_embedding, restrict=pool
        )
        # Keep any pool members the rerank channels didn't score, in pool order.
        ordered_pool = reranked + [no for no in pool if no not in set(reranked)]

        # Step 5: topK seeds.
        seed_nos = ordered_pool[:top_k]
        seed_nodes = _load_nodes(session, seed_nos)
        result.seeds = [
            _node_result(seed_nodes[no]) for no in seed_nos if no in seed_nodes
        ]

        # Step 6: one-hop edges of the seeds.
        hop_edges = _one_hop_edges(session, seed_nos)
        seed_set = set(seed_nos)
        neighbor_candidate_nos: List[str] = []
        for e in hop_edges:
            for endpoint in (e.source_node_no, e.target_node_no):
                if endpoint not in seed_set and endpoint not in neighbor_candidate_nos:
                    neighbor_candidate_nos.append(endpoint)

        # Step 7: batch-fetch neighbor node details.
        neighbor_nodes = _load_nodes(session, neighbor_candidate_nos)

        # Step 8: rerank neighbors against the original query (neighborTopK).
        ranked_neighbors = _hybrid_recall(
            session,
            query,
            NODE,
            max(neighbor_k * 4, 12),
            result.warnings,
            query_embedding,
            restrict=neighbor_candidate_nos,
        )
        ordered_neighbors = ranked_neighbors + [
            no for no in neighbor_candidate_nos if no not in set(ranked_neighbors)
        ]
        neighbor_nos = ordered_neighbors[:neighbor_k]
        result.neighbors = [
            _node_result(neighbor_nodes[no])
            for no in neighbor_nos
            if no in neighbor_nodes
        ]

        # Edges connecting the returned seeds+neighbors, for context.
        keep = seed_set | set(neighbor_nos)
        result.edges = [
            _edge_result(e)
            for e in hop_edges
            if e.source_node_no in keep and e.target_node_no in keep
        ]

        # Step 9: resolve Ref -> original lines.
        merged_ref: Dict[str, set] = {}

        def _accumulate(ref: Dict) -> None:
            for doc_no, tokens in (ref or {}).items():
                merged_ref.setdefault(doc_no, set()).update(tokens or [])

        for nr in result.seeds + result.neighbors:
            _accumulate(nr.ref)
        for er in result.edges:
            _accumulate(er.ref)

        by_doc = _collect_line_numbers(
            {d: sorted(t) for d, t in merged_ref.items()}, window
        )
        result.evidence = _fetch_lines(session, by_doc)

    # Step 10: return.
    return result
