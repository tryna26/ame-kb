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

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .embed import embed_query, embedding_available
from .models import DocChunk, DocLine, GraphEdge, GraphNode
from .queryexpand import expand_queries
from .rrf import rrf_merge
from .searchbackend import SearchFilters, get_index
from .vecmath import cosine  # noqa: F401  (re-exported for tests/back-compat)

NODE = "NODE"
EDGE = "EDGE"
DOC_CHUNK = "DOC_CHUNK"
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
class DocChunkHit:
    chunk_no: str
    doc_no: str
    chunk_index: int
    content: str
    origin_url: str
    file_path: str
    line_start: int
    line_end: int


@dataclass
class RecallResult:
    query: str
    seeds: List[NodeResult] = field(default_factory=list)
    neighbors: List[NodeResult] = field(default_factory=list)
    edges: List[EdgeResult] = field(default_factory=list)
    evidence: List[EvidenceLine] = field(default_factory=list)
    doc_chunks: List[DocChunkHit] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


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


@dataclass
class RecallTier:
    """Tunables for one retry pass (sources.md:86-97 retry ladder).

    graph_scope is deliberately absent: it is NEVER relaxed across tiers. Only
    score thresholds are lowered and, at the last tier, the (dormant) workspace
    filter is dropped.
    """

    min_score_text: float
    min_score_embedding: float
    workspace_ids: Optional[Sequence[str]] = None
    drop_workspace: bool = False


def _default_tier() -> "RecallTier":
    s = get_settings()
    return RecallTier(
        min_score_text=s.min_score_text,
        min_score_embedding=s.min_score_embedding,
    )


def _hybrid_recall(
    query: str,
    object_type: str,
    limit: int,
    warnings: List[str],
    query_embedding: Optional[Sequence[float]] = None,
    restrict: Optional[Sequence[str]] = None,
    ignore_graph_scope: bool = False,
    tier: Optional["RecallTier"] = None,
) -> List[str]:
    """One search through the HybridIndex port: text + (optional) vector fused
    with RRF, a failing channel only warns (node_retriever.go:332-341).

    The concrete backend (MySQL FULLTEXT+cosine, or RediSearch) is selected by
    SEARCH_BACKEND and hidden behind get_index(); recall never touches SQL or a
    vector store directly. `tier` overrides the score thresholds / workspace
    filter for a retry pass; None uses the configured defaults.
    """
    settings = get_settings()
    if tier is None:
        tier = _default_tier()
    filters = SearchFilters(
        graph_no=settings.graph_no,
        graph_version=settings.graph_version,
        object_type=object_type,
        restrict_object_nos=restrict,
        workspace_ids=None if tier.drop_workspace else tier.workspace_ids,
        ignore_graph_scope=ignore_graph_scope,
    )
    return get_index().search(
        query,
        filters=filters,
        limit=limit,
        min_score_text=tier.min_score_text,
        min_score_embedding=tier.min_score_embedding,
        query_embedding=query_embedding,
        warnings=warnings,
    )


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


def _doc_chunk_channel(
    session: Session,
    query: str,
    query_embedding: Optional[Sequence[float]],
    warnings: List[str],
) -> List[DocChunkHit]:
    """Third fallback channel: retrieve doc chunks directly.

    Deliberately NOT bound by graph_scope (sources.md ④b: kg_doc_chunk fallback,
    top_k=10), so relevant source passages surface even when the graph is sparse
    or the query doesn't hit any node/edge.
    """
    settings = get_settings()
    hits = _hybrid_recall(
        query,
        DOC_CHUNK,
        settings.doc_chunk_topk,
        warnings,
        query_embedding,
        ignore_graph_scope=True,
    )
    if not hits:
        return []
    rows = (
        session.execute(
            select(DocChunk).where(DocChunk.chunk_no.in_(list(hits)))
        )
        .scalars()
        .all()
    )
    by_no = {c.chunk_no: c for c in rows}
    out: List[DocChunkHit] = []
    for cno in hits:  # preserve fused rank order
        c = by_no.get(cno)
        if c is None:
            continue
        out.append(
            DocChunkHit(
                chunk_no=c.chunk_no,
                doc_no=c.doc_no,
                chunk_index=c.chunk_index,
                content=c.content or "",
                origin_url=c.origin_url or "",
                file_path=c.file_path or "",
                line_start=c.line_start or 0,
                line_end=c.line_end or 0,
            )
        )
    return out


def _embed_query(query: str, warnings: List[str]) -> Optional[List[float]]:
    if not embedding_available():
        return None
    try:
        return embed_query(query)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"query embedding failed: {exc}")
        return None


def _candidate_pool_ordered(
    session: Session,
    query: str,
    query_embedding: Optional[Sequence[float]],
    wide: int,
    warnings: List[str],
    tier: Optional["RecallTier"] = None,
) -> List[str]:
    """Steps 1-4 for one query: hybrid-recall NODE+EDGE, build the candidate
    pool (both edge endpoints in), rerank within the pool. Returns the pool in
    reranked order (unscored members appended in pool order)."""
    node_hits = _hybrid_recall(
        query, NODE, wide, warnings, query_embedding, tier=tier
    )
    edge_hits = _hybrid_recall(
        query, EDGE, wide, warnings, query_embedding, tier=tier
    )

    edges_by_no = _load_edges(session, edge_hits)
    endpoints = {
        eno: (e.source_node_no, e.target_node_no) for eno, e in edges_by_no.items()
    }
    pool = build_candidate_pool(node_hits, edge_hits, endpoints)

    reranked = _hybrid_recall(
        query, NODE, wide, warnings, query_embedding, restrict=pool, tier=tier
    )
    return reranked + [no for no in pool if no not in set(reranked)]


def _retry_ladder() -> List["RecallTier"]:
    """Build the retry tiers (sources.md:86-97). graph_scope is never relaxed.

    Tier 1: strict thresholds (default 0.8). Tier 2: configured (lower)
    thresholds. Tier 3: drop the (dormant) workspace filter, thresholds at 0.
    Only used when RECALL_MIN_RESULTS > 0; otherwise recall does a single pass
    at the configured defaults (V3 behaviour).
    """
    s = get_settings()
    return [
        RecallTier(
            min_score_text=s.retry_strict_text,
            min_score_embedding=s.retry_strict_embedding,
        ),
        RecallTier(
            min_score_text=s.min_score_text,
            min_score_embedding=s.min_score_embedding,
        ),
        RecallTier(min_score_text=0.0, min_score_embedding=0.0, drop_workspace=True),
    ]


def _expand_neighbors(
    session: Session,
    query: str,
    query_embedding: Optional[Sequence[float]],
    seed_nos: Sequence[str],
    neighbor_k: int,
    max_hops: int,
    warnings: List[str],
) -> Tuple[List[str], List[GraphEdge], Dict[str, GraphNode]]:
    """Bounded, gap-driven multi-hop neighbor expansion.

    Frontier BFS from the seeds. Each hop: one-hop edges of the frontier, rerank
    the new (unvisited) neighbor candidates against the query, keep neighbor_k.
    A `visited` set prevents cycles (graph_explorer.go markExploredEntityIDs). We
    stop early once we already have neighbor_k results (gap-driven), and never
    exceed max_hops. At max_hops=1 this reproduces V3 steps 6-8 exactly.

    Returns (ordered neighbor_nos, all traversed edges, loaded node map).
    """
    seed_set = set(seed_nos)
    visited = set(seed_nos)
    frontier: List[str] = list(seed_nos)
    all_edges: List[GraphEdge] = []
    node_cache: Dict[str, GraphNode] = {}
    picked: List[str] = []  # neighbor_nos in rank order, deduped

    hops = max(1, max_hops)
    for _ in range(hops):
        if not frontier:
            break
        hop_edges = _one_hop_edges(session, frontier)
        all_edges.extend(hop_edges)

        candidates: List[str] = []
        for e in hop_edges:
            for endpoint in (e.source_node_no, e.target_node_no):
                if endpoint not in visited and endpoint not in candidates:
                    candidates.append(endpoint)
        if not candidates:
            break

        node_cache.update(_load_nodes(session, candidates))
        ranked = _hybrid_recall(
            query,
            NODE,
            max(neighbor_k * 4, 12),
            warnings,
            query_embedding,
            restrict=candidates,
        )
        ordered = ranked + [no for no in candidates if no not in set(ranked)]

        next_frontier: List[str] = []
        for no in ordered:
            if no in visited:
                continue
            visited.add(no)
            next_frontier.append(no)
            if no not in picked:
                picked.append(no)
        # Gap-driven: stop once we already have enough neighbors.
        if len(picked) >= neighbor_k:
            break
        frontier = next_frontier

    neighbor_nos = picked[:neighbor_k]
    # Ensure all kept neighbors have node details loaded.
    missing = [no for no in neighbor_nos if no not in node_cache]
    if missing:
        node_cache.update(_load_nodes(session, missing))
    return neighbor_nos, all_edges, node_cache


def recall(query: str, window: int = 0) -> RecallResult:
    settings = get_settings()
    result = RecallResult(query=query)

    query_embedding = _embed_query(query, result.warnings)

    top_k = settings.recall_topk
    neighbor_k = settings.recall_neighbor_topk
    # A wider candidate limit than top_k so reranking has room to work.
    wide = max(top_k * 4, 20)

    # Multi-query: expand into paraphrases/sub-questions (off by default), run
    # the pool-building steps per query, then RRF-fuse the orderings
    # (rag_retrieve.go:149-194). Single-query keeps V3 behaviour exactly.
    queries = expand_queries(query, result.warnings)

    def _build_pool(session, tier: Optional["RecallTier"]) -> List[str]:
        pools: List[List[str]] = []
        for q in queries:
            q_emb = query_embedding if q == query else _embed_query(q, result.warnings)
            pools.append(
                _candidate_pool_ordered(
                    session, q, q_emb, wide, result.warnings, tier=tier
                )
            )
        return pools[0] if len(pools) == 1 else rrf_merge(pools)

    with session_scope() as session:
        # Retry ladder (sources.md:86-97): only when RECALL_MIN_RESULTS>0, retry
        # with progressively looser thresholds until enough seeds are found.
        # graph_scope is NEVER relaxed. Default (min_results=0) is a single pass
        # at the configured thresholds == V3 behaviour.
        min_results = settings.recall_min_results
        if min_results <= 0:
            ordered_pool = _build_pool(session, tier=None)
        else:
            ordered_pool = []
            for i, tier in enumerate(_retry_ladder()):
                ordered_pool = _build_pool(session, tier=tier)
                if len(ordered_pool) >= min_results:
                    break
                if i > 0:
                    result.warnings.append(
                        f"retry ladder tier {i + 1}: "
                        f"{len(ordered_pool)}/{min_results} results"
                    )

        # Step 5: topK seeds.
        seed_nos = ordered_pool[:top_k]
        seed_nodes = _load_nodes(session, seed_nos)
        result.seeds = [
            _node_result(seed_nodes[no]) for no in seed_nos if no in seed_nodes
        ]

        # Steps 6-8: bounded multi-hop neighbor expansion (visited-set, gap
        # driven). At RECALL_MAX_HOPS=1 this is exactly V3's one-hop rerank.
        seed_set = set(seed_nos)
        neighbor_nos, hop_edges, neighbor_nodes = _expand_neighbors(
            session,
            query,
            query_embedding,
            seed_nos,
            neighbor_k,
            settings.recall_max_hops,
            result.warnings,
        )
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

        # Doc-chunk fallback channel (not bound by graph_scope).
        result.doc_chunks = _doc_chunk_channel(
            session, query, query_embedding, result.warnings
        )

    # Step 10: return.
    return result
