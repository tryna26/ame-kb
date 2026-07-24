"""Offline tests for stateful progressive exploration (recall_state + the
`exclude` path in _expand_neighbors). No real DB/LLM/embedding calls."""
import time

import ame_kb.recall_state as rs


# ---- in-process explored-state store ---------------------------------------


class _S:
    def __init__(self, ttl=1800.0, max_states=1000):
        self.recall_state_ttl_seconds = ttl
        self.recall_state_max_states = max_states


def _wire(monkeypatch, settings):
    monkeypatch.setattr(rs, "get_settings", lambda: settings)
    rs.reset_all()


def test_state_add_and_get(monkeypatch):
    _wire(monkeypatch, _S())
    assert rs.get_explored("sid") == set()
    total = rs.mark_explored("sid", ["a", "b"])
    assert total == 2
    assert rs.get_explored("sid") == {"a", "b"}
    # Adds union, dedup, and drop empties.
    total = rs.mark_explored("sid", ["b", "c", ""])
    assert total == 3
    assert rs.get_explored("sid") == {"a", "b", "c"}


def test_state_isolated_by_id(monkeypatch):
    _wire(monkeypatch, _S())
    rs.mark_explored("s1", ["a"])
    rs.mark_explored("s2", ["b"])
    assert rs.get_explored("s1") == {"a"}
    assert rs.get_explored("s2") == {"b"}


def test_state_key_namespaces_by_graph(monkeypatch):
    # The same caller-supplied state_id must not cross-exclude nodes between
    # different graphs/versions: node_nos are only unique within a graph.
    _wire(monkeypatch, _S())
    k1 = rs.state_key("graph_a", 1, "sess")
    k2 = rs.state_key("graph_b", 1, "sess")
    k3 = rs.state_key("graph_a", 2, "sess")
    assert len({k1, k2, k3}) == 3
    rs.mark_explored(k1, ["n1"])
    assert rs.get_explored(k2) == set()  # other graph, same state_id
    assert rs.get_explored(k3) == set()  # other version, same state_id
    assert rs.get_explored(k1) == {"n1"}


def test_state_clear(monkeypatch):
    _wire(monkeypatch, _S())
    rs.mark_explored("sid", ["a"])
    assert rs.clear_state("sid") is True
    assert rs.get_explored("sid") == set()
    assert rs.clear_state("missing") is False


def test_state_ttl_eviction(monkeypatch):
    _wire(monkeypatch, _S(ttl=0.05))
    rs.mark_explored("sid", ["a"])
    assert rs.get_explored("sid") == {"a"}
    time.sleep(0.06)
    # Past TTL: the state is evicted and reads empty.
    assert rs.get_explored("sid") == set()


def test_state_capacity_evicts_lru(monkeypatch):
    _wire(monkeypatch, _S(max_states=2))
    rs.mark_explored("s1", ["a"])
    time.sleep(0.01)
    rs.mark_explored("s2", ["b"])
    time.sleep(0.01)
    # Touch s1 so s2 becomes least-recently-used.
    rs.get_explored("s1")
    time.sleep(0.01)
    rs.mark_explored("s3", ["c"])  # over cap -> drop LRU (s2)
    active = set(rs.active_states())
    assert "s3" in active and "s1" in active
    assert "s2" not in active


# ---- _expand_neighbors honours `exclude` -----------------------------------


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
        self.description = None
        self.properties = {}
        self.ref = {}


def _setup_graph(monkeypatch, adjacency, ranking):
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


def test_expand_excludes_already_explored(monkeypatch):
    # seed s -> a, b, c. Without exclude all three are neighbors; excluding
    # {a} yields only b, c (a's edge is still traversed but a is not kept).
    adjacency = {"s": ["a", "b", "c"]}
    ranking = ["a", "b", "c"]
    r = _setup_graph(monkeypatch, adjacency, ranking)

    baseline, _e, _n = r._expand_neighbors(
        session=None, query="q", query_embedding=None, seed_nos=["s"],
        neighbor_k=5, max_hops=1, warnings=[],
    )
    assert set(baseline) == {"a", "b", "c"}

    with_exclude, _e2, _n2 = r._expand_neighbors(
        session=None, query="q", query_embedding=None, seed_nos=["s"],
        neighbor_k=5, max_hops=1, warnings=[], exclude={"a"},
    )
    assert set(with_exclude) == {"b", "c"}
    assert "a" not in with_exclude


def test_expand_empty_exclude_is_noop(monkeypatch):
    adjacency = {"s": ["a", "b"]}
    ranking = ["a", "b"]
    r = _setup_graph(monkeypatch, adjacency, ranking)
    nos, _e, _n = r._expand_neighbors(
        session=None, query="q", query_embedding=None, seed_nos=["s"],
        neighbor_k=5, max_hops=1, warnings=[], exclude=set(),
    )
    assert set(nos) == {"a", "b"}
