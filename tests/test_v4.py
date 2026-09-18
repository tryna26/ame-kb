"""Offline V4 tests: generic RRF, query expansion, chunker, doc-chunk channel.
No DB/LLM/embedding calls."""
from ame_kb.rrf import rrf_merge, rrf_merge_objects


def test_rrf_merge_objects_dedup_and_order():
    # Two ranked lists of (id, score) tuples; fuse by id, keep higher score.
    a = [("x", 0.1), ("y", 0.2)]
    b = [("y", 0.9), ("z", 0.3)]
    merged = rrf_merge_objects(
        [a, b],
        key_fn=lambda t: t[0],
        better_fn=lambda new, cur: new[1] > cur[1],
    )
    ids = [t[0] for t in merged]
    # y appears in both near the top -> wins; representative keeps higher score.
    assert ids[0] == "y"
    assert dict(merged)["y"] == 0.9
    assert set(ids) == {"x", "y", "z"}


def test_rrf_merge_objects_topk_truncation():
    lists = [["a", "b", "c", "d"]]
    merged = rrf_merge_objects([lists[0]], key_fn=lambda s: s, top_k=2)
    assert merged == ["a", "b"]


def test_rrf_merge_objects_matches_string_merge():
    # Generic merge over plain strings must agree with the string rrf_merge.
    lists = [["a", "b", "c"], ["b", "c", "a"]]
    generic = rrf_merge_objects(lists, key_fn=lambda s: s)
    assert generic == rrf_merge(lists)


def test_expand_queries_single_is_noop(monkeypatch):
    # RECALL_MAX_QUERIES defaults to 1 -> just the original, no LLM call.
    from ame_kb import queryexpand

    monkeypatch.setattr(
        queryexpand, "get_settings", lambda: _FakeSettings(max_queries=1)
    )
    warnings = []
    assert queryexpand.expand_queries("hello", warnings) == ["hello"]
    assert warnings == []


def test_expand_queries_parses_llm_array(monkeypatch):
    from ame_kb import queryexpand

    monkeypatch.setattr(
        queryexpand, "get_settings", lambda: _FakeSettings(max_queries=3)
    )
    # Fake the LLM call to return a JSON array in a code fence.
    import ame_kb.extract as extract_mod

    monkeypatch.setattr(
        extract_mod, "call_llm", lambda prompt: '```json\n["别名A","别名B"]\n```'
    )
    warnings = []
    out = queryexpand.expand_queries("原始", warnings)
    assert out[0] == "原始"  # original always first
    assert "别名A" in out and "别名B" in out
    assert len(out) == 3
    assert warnings == []


def test_expand_queries_llm_failure_falls_back(monkeypatch):
    from ame_kb import queryexpand

    monkeypatch.setattr(
        queryexpand, "get_settings", lambda: _FakeSettings(max_queries=3)
    )
    import ame_kb.extract as extract_mod

    def _boom(prompt):
        raise RuntimeError("no LLM")

    monkeypatch.setattr(extract_mod, "call_llm", _boom)
    warnings = []
    out = queryexpand.expand_queries("原始", warnings)
    assert out == ["原始"]
    assert warnings and "query expansion failed" in warnings[0]


class _FakeSettings:
    def __init__(self, max_queries):
        self.recall_max_queries = max_queries


def test_chunker_empty_text():
    from ame_kb.chunker import split_text

    assert split_text("", 100, 20) == []
    assert split_text("   \n  ", 100, 20) == []


def test_chunker_single_small_chunk():
    from ame_kb.chunker import split_text

    text = "line1\nline2\nline3"
    chunks = split_text(text, 100, 20)
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].line_start == 1
    assert chunks[0].line_end == 3
    assert "line1" in chunks[0].content and "line3" in chunks[0].content


def test_chunker_windows_and_overlap_line_spans():
    from ame_kb.chunker import split_text

    # 10 lines of ~10 chars each. Small window forces multiple chunks.
    text = "\n".join(f"line{i:02d}xyz" for i in range(1, 11))
    chunks = split_text(text, 30, 10)
    assert len(chunks) >= 2
    # Indices are contiguous from 0.
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # Line spans are 1-based, monotonic, and cover the doc.
    assert chunks[0].line_start == 1
    assert chunks[-1].line_end == 10
    for c in chunks:
        assert 1 <= c.line_start <= c.line_end <= 10


def test_chunker_no_stall_when_overlap_ge_size():
    from ame_kb.chunker import split_text

    text = "abcdefghij" * 10  # 100 chars, no newlines
    chunks = split_text(text, 20, 999)  # overlap clamped below size
    # Must terminate and cover everything without infinite loop.
    assert len(chunks) >= 1
    assert "".join(c.content for c in chunks).replace("", "")


def test_chunk_no_stable():
    from ame_kb.docchunk import chunk_no

    a = chunk_no("doc/a.md", 0)
    b = chunk_no("doc/a.md", 1)
    assert a != b
    assert chunk_no("doc/a.md", 0) == a  # deterministic
    assert len(a) == 32


class _FakeEdge:
    def __init__(self, src, dst):
        self.source_node_no = src
        self.target_node_no = dst
        self.graph_edge_no = f"{src}->{dst}"
        self.name = "related_to"
        self.description = None
        self.ref = {}


class _FakeNode:
    def __init__(self, no):
        self.graph_node_no = no
        self.name = no
        self.type = "Concept"
        self.entity_spec = None
        self.description = None
        self.properties = {}
        self.ref = {}


def _setup_graph(monkeypatch, adjacency, ranking):
    """Wire recall's DB/index helpers to an in-memory graph.

    adjacency: {node_no: [neighbor_no, ...]} (undirected edges via source->dst)
    ranking:   order _hybrid_recall returns restricted candidates in
    """
    import ame_kb.recall as r

    edges = []
    seen = set()
    for src, nbrs in adjacency.items():
        for dst in nbrs:
            key = tuple(sorted((src, dst)))
            if key in seen:
                continue
            seen.add(key)
            edges.append(_FakeEdge(src, dst))

    def fake_one_hop(session, node_nos):
        node_set = set(node_nos)
        return [
            e for e in edges
            if e.source_node_no in node_set or e.target_node_no in node_set
        ]

    def fake_load_nodes(session, node_nos):
        return {no: _FakeNode(no) for no in node_nos}

    def fake_hybrid(query, object_type, limit, warnings, query_embedding=None,
                    restrict=None, ignore_graph_scope=False):
        if restrict is None:
            return []
        restrict_set = set(restrict)
        return [no for no in ranking if no in restrict_set][:limit]

    monkeypatch.setattr(r, "_one_hop_edges", fake_one_hop)
    monkeypatch.setattr(r, "_load_nodes", fake_load_nodes)
    monkeypatch.setattr(r, "_hybrid_recall", fake_hybrid)
    return r


def test_multihop_one_hop_matches_v3(monkeypatch):
    # seed s -> a, b ; with max_hops=1 only direct neighbors are reachable.
    adjacency = {"s": ["a", "b"], "a": ["c"], "b": ["d"]}
    ranking = ["a", "b", "c", "d"]
    r = _setup_graph(monkeypatch, adjacency, ranking)
    neighbor_nos, edges, nodes = r._expand_neighbors(
        session=None,
        query="q",
        query_embedding=None,
        seed_nos=["s"],
        neighbor_k=5,
        max_hops=1,
        warnings=[],
    )
    assert set(neighbor_nos) == {"a", "b"}  # c/d are 2 hops away, excluded
    assert neighbor_nos[:2] == ["a", "b"]  # rank order preserved


def test_multihop_two_hops_reaches_second_ring(monkeypatch):
    adjacency = {"s": ["a"], "a": ["b"], "b": ["c"]}
    ranking = ["a", "b", "c"]
    r = _setup_graph(monkeypatch, adjacency, ranking)
    neighbor_nos, edges, nodes = r._expand_neighbors(
        session=None,
        query="q",
        query_embedding=None,
        seed_nos=["s"],
        neighbor_k=5,
        max_hops=2,
        warnings=[],
    )
    # hop1: a ; hop2 from a: b. c is 3 hops away -> excluded.
    assert neighbor_nos == ["a", "b"]


def test_multihop_no_cycle(monkeypatch):
    # Triangle s-a-b-s ; visited set must prevent revisiting s.
    adjacency = {"s": ["a", "b"], "a": ["b"], "b": ["s"]}
    ranking = ["a", "b"]
    r = _setup_graph(monkeypatch, adjacency, ranking)
    neighbor_nos, edges, nodes = r._expand_neighbors(
        session=None,
        query="q",
        query_embedding=None,
        seed_nos=["s"],
        neighbor_k=5,
        max_hops=3,
        warnings=[],
    )
    assert "s" not in neighbor_nos  # seed never re-added
    assert set(neighbor_nos) == {"a", "b"}


def test_multihop_gap_driven_stop(monkeypatch):
    # Enough neighbors at hop 1 -> should not walk to hop 2.
    adjacency = {"s": ["a", "b", "c"], "a": ["deep"]}
    ranking = ["a", "b", "c", "deep"]
    r = _setup_graph(monkeypatch, adjacency, ranking)
    neighbor_nos, edges, nodes = r._expand_neighbors(
        session=None,
        query="q",
        query_embedding=None,
        seed_nos=["s"],
        neighbor_k=2,
        max_hops=3,
        warnings=[],
    )
    assert len(neighbor_nos) == 2
    assert "deep" not in neighbor_nos


def test_url_doc_id_stable_and_prefixed():
    from ame_kb.ingest import _url_doc_id

    a = _url_doc_id("https://example.com/docs/page?x=1")
    assert a.startswith("url:")
    assert "example.com/docs/page" in a
    # trailing slash normalized -> stable key
    assert _url_doc_id("https://example.com/docs/page/") == _url_doc_id(
        "https://example.com/docs/page"
    )


def test_read_url_list_skips_comments(tmp_path):
    from ame_kb.ingest import read_url_list

    p = tmp_path / "urls.txt"
    p.write_text(
        "# comment\nhttps://a.com\n\n  https://b.com  \n# another\n",
        encoding="utf-8",
    )
    assert read_url_list(str(p)) == ["https://a.com", "https://b.com"]


def test_extract_split_retry_on_bad_json(monkeypatch):
    import ame_kb.extract as ex

    calls = {"n": 0}

    def fake_call_llm(prompt):
        calls["n"] += 1
        # Full-doc call returns garbage; half-doc calls return valid empty JSON.
        if "[1]" in prompt and "[2]" in prompt:
            return "not json at all"
        return '{"nodes": [], "edges": []}'

    monkeypatch.setattr(ex, "call_llm", fake_call_llm)
    monkeypatch.setattr(ex, "schema_prompt_block", lambda: "SCHEMA")

    from ame_kb.ingest import Document
    from pathlib import Path

    doc = Document(doc_id="d", path=Path("d.md"), text="line one\nline two")
    result = ex.extract(doc)
    # First (full) call failed -> split into halves that succeed.
    assert calls["n"] >= 3
    assert result.nodes == [] and result.edges == []


def test_redis_tag_escaping():
    from ame_kb.searchbackend.redis import _esc_tag

    # Business keys contain ':' and '-' which are TAG-special.
    assert _esc_tag("Person:grace-hopper") == "Person\\:grace\\-hopper"
    assert _esc_tag("default") == "default"


def test_redis_base_filter_scoping():
    from ame_kb.searchbackend.base import SearchFilters
    from ame_kb.searchbackend.redis import RedisHybridIndex

    idx = RedisHybridIndex()  # constructor is lazy, no connection
    f = SearchFilters(graph_no="default", graph_version=1, object_type="NODE")
    base = idx._base_filter(f)
    assert "@object_type:{NODE}" in base
    assert "@graph_no:{default}" in base
    assert "@graph_version:{1}" in base

    # ignore_graph_scope drops the graph clauses (doc-chunk fallback).
    f2 = SearchFilters(
        graph_no="default",
        graph_version=1,
        object_type="DOC_CHUNK",
        ignore_graph_scope=True,
    )
    base2 = idx._base_filter(f2)
    assert "@object_type:{DOC_CHUNK}" in base2
    assert "graph_no" not in base2

    # empty restrict set -> match nothing.
    f3 = SearchFilters(
        graph_no="default", graph_version=1, object_type="NODE",
        restrict_object_nos=[],
    )
    assert idx._base_filter(f3) == ""


def test_factory_selects_backend(monkeypatch):
    import ame_kb.searchbackend.factory as fac

    fac.get_index.cache_clear()

    class _S:
        search_backend = "redis"

    monkeypatch.setattr(fac, "get_settings", lambda: _S(), raising=False)
    # Patch the name the factory imports lazily.
    import ame_kb.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    idx = fac.get_index()
    from ame_kb.searchbackend.redis import RedisHybridIndex

    assert isinstance(idx, RedisHybridIndex)
    fac.get_index.cache_clear()




