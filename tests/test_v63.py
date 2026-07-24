"""Offline V6.3 tests: the service facade's graph-context resolution, the recall
session trace fields, and the REST API endpoints (service mocked, no DB/LLM).
"""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import ame_kb.service as service_mod
from ame_kb.api import create_app
from ame_kb.config import clear_graph_context, get_settings
from ame_kb.recall import HopTrace, RecallResult, RecallSession
from ame_kb.query import NodeHit
from ame_kb.graphs import GraphInfo
from ame_kb.pipeline import EnqueueResult


@pytest.fixture(autouse=True)
def _reset_graph_context():
    clear_graph_context()
    yield
    clear_graph_context()


# ---- service graph-context resolution -------------------------------------


def test_service_search_enters_resolved_graph_context(monkeypatch):
    seen = {}

    def _fake_recall(query, window=0, trace=False, state_id=None):
        s = get_settings()
        seen["graph_no"] = s.graph_no
        seen["graph_version"] = s.graph_version
        seen["trace"] = trace
        return RecallResult(query=query)

    # Named graph, no explicit version -> resolve latest ACTIVE.
    monkeypatch.setattr(service_mod, "_recall", _fake_recall)
    monkeypatch.setattr(
        service_mod.graphs_mod, "_latest_version_safe", lambda g: 7
    )

    service_mod.search("q", graph_no="graph_abc", trace=True)
    assert seen == {"graph_no": "graph_abc", "graph_version": 7, "trace": True}
    # Context must not leak after the call: back to the environment default.
    restored = get_settings()
    assert (restored.graph_no, restored.graph_version) != ("graph_abc", 7)


def test_service_search_inherits_callback_context(monkeypatch):
    """No explicit graph_no -> inherit the context the CLI callback set,
    instead of falling back to the environment default (regression)."""
    from ame_kb import graphs as graphs_mod

    seen = {}

    def _fake_recall(query, window=0, trace=False, state_id=None):
        s = get_settings()
        seen["graph_no"] = s.graph_no
        seen["graph_version"] = s.graph_version
        return RecallResult(query=query)

    monkeypatch.setattr(service_mod, "_recall", _fake_recall)
    monkeypatch.setattr(graphs_mod, "_latest_version_safe", lambda g: 42)

    # Simulate `--graph-no graph_xyz search "..."`: callback sets context, and
    # the command calls service.search WITHOUT re-passing graph_no.
    graphs_mod.apply_graph_context("graph_xyz", None)
    service_mod.search("q")
    assert seen == {"graph_no": "graph_xyz", "graph_version": 42}


def test_service_search_defaults_do_not_query_kg_graph(monkeypatch):
    def _boom(graph_no):
        raise AssertionError("default graph must not resolve a version")

    monkeypatch.setattr(service_mod.graphs_mod, "_latest_version_safe", _boom)
    monkeypatch.setattr(
        service_mod, "_recall",
        lambda query, window=0, trace=False, state_id=None: RecallResult(query)
    )
    # The default-graph sentinel keeps legacy behaviour: never a kg_graph lookup.
    service_mod.search("q", graph_no=service_mod.DEFAULT_GRAPH_NO)


def test_service_enqueue_rejects_unmanaged_graph(monkeypatch):
    monkeypatch.setattr(service_mod.graphs_mod, "is_managed", lambda g: False)
    monkeypatch.setattr(
        service_mod.graphs_mod, "_latest_version_safe", lambda g: 1
    )
    with pytest.raises(ValueError):
        service_mod.enqueue_ingest("graph_x")


# ---- recall session trace (unit, pure dataclasses) ------------------------


def test_recall_session_trace_shape():
    trace = RecallSession(query="q", embedding_available=True)
    trace.queries = ["q", "q2"]
    trace.pool_sizes = [5, 3]
    trace.hops.append(HopTrace(hop=1, candidates=4, picked=2))
    assert trace.hops[0].picked == 2
    assert trace.queries == ["q", "q2"]


# ---- REST API (service mocked) --------------------------------------------


@pytest.fixture
def client():
    return TestClient(create_app())


def test_api_search_returns_session(client, monkeypatch):
    res = RecallResult(query="hello")
    res.session = RecallSession(query="hello", embedding_available=False)
    monkeypatch.setattr(
        service_mod,
        "search",
        lambda query, **kw: res,
    )
    r = client.post("/search", json={"query": "hello", "graph_no": "g1"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "hello"
    assert body["session"]["query"] == "hello"


def test_api_entities(client, monkeypatch):
    monkeypatch.setattr(
        service_mod,
        "find_entities",
        lambda name, **kw: [NodeHit("Asset:x", "X", "Asset", {})],
    )
    r = client.get("/graphs/g1/entities", params={"name": "X"})
    assert r.status_code == 200
    assert r.json()[0]["graph_node_no"] == "Asset:x"


def test_api_list_graphs(client, monkeypatch):
    monkeypatch.setattr(
        service_mod,
        "list_graphs",
        lambda: [GraphInfo("g1", 2, "G1", "ACTIVE")],
    )
    r = client.get("/graphs")
    assert r.status_code == 200
    assert r.json() == [
        {"graph_no": "g1", "graph_version": 2, "name": "G1", "status": "ACTIVE"}
    ]


def test_api_ingest_accepts(client, monkeypatch):
    monkeypatch.setattr(
        service_mod,
        "enqueue_ingest",
        lambda graph_no, **kw: EnqueueResult("task_1", True, None),
    )
    r = client.post("/graphs/g1/ingest", json={"force": True})
    assert r.status_code == 202
    assert r.json()["task_no"] == "task_1"


def test_api_ingest_unmanaged_is_400(client, monkeypatch):
    def _raise(graph_no, **kw):
        raise ValueError("not a managed graph: g1")

    monkeypatch.setattr(service_mod, "enqueue_ingest", _raise)
    r = client.post("/graphs/g1/ingest", json={})
    assert r.status_code == 400
    assert "managed" in r.json()["detail"]


def test_api_task_not_found_is_404(client, monkeypatch):
    def _raise(task_no):
        raise ValueError("unknown task: t1")

    monkeypatch.setattr(service_mod, "task_status", _raise)
    r = client.get("/tasks/t1")
    assert r.status_code == 404


def test_api_task_retry(client, monkeypatch):
    called = {}
    monkeypatch.setattr(
        service_mod,
        "retry_task",
        lambda task_no, max_attempts=None: called.update(
            task_no=task_no, max_attempts=max_attempts
        ),
    )
    r = client.post("/tasks/t1/retry", json={"max_attempts": 5})
    assert r.status_code == 200
    assert called == {"task_no": "t1", "max_attempts": 5}
