"""Phase-1 code extraction: tree-sitter golden structure + ontology projection.

Extraction tests write tiny Python/Go repos to tmp_path and assert the CodeGraph
node kinds, signatures, lines, and Contains/Import/SubPackage edges. The
projection test runs project_code_graph against in-memory SQLite (mirroring
test_v6's factory) and checks the Asset/Implementation node_no namespace,
EXTRACTED edges, and search-index rows.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from ame_kb.code.codegraph import EdgeKind, NodeKind, from_json, to_json
from ame_kb.code.treesitter.builder import build_code_graph


# ── helpers ─────────────────────────────────────────────────────────────────────

def _by_id(graph):
    return {n.id: n for n in graph.nodes}


def _edge_set(graph):
    return {(e.from_, e.kind.value, e.to) for e in graph.edges}


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ── Python extraction ────────────────────────────────────────────────────────────

def test_python_extraction_golden(tmp_path):
    _write(
        tmp_path,
        "pkg/mod.py",
        '''import os
from pkg import helper


def top_level(a, b):
    """A top-level function."""
    return a + b


class Widget:
    """A widget."""

    def render(self):
        return 1
''',
    )
    _write(tmp_path, "pkg/helper.py", "def helq():\n    return 0\n")

    graph = build_code_graph(str(tmp_path), ["python"])
    by_id = _by_id(graph)

    # Symbols: kinds + qualified ids.
    assert by_id["pkg/top_level"].kind is NodeKind.FUNCTION
    assert by_id["pkg/Widget"].kind is NodeKind.STRUCT
    assert by_id["pkg/Widget.render"].kind is NodeKind.METHOD  # method carries class prefix

    # Signature is the header without the body; docstring captured.
    assert by_id["pkg/top_level"].signature.startswith("def top_level(a, b)")
    assert "return" not in by_id["pkg/top_level"].signature
    assert by_id["pkg/top_level"].doc == "A top-level function."
    assert by_id["pkg/top_level"].line == 5  # 1-based

    # Containment: package -> file -> symbol.
    edges = _edge_set(graph)
    assert ("pkg", EdgeKind.CONTAINS.value, "pkg/mod.py") in edges
    assert ("pkg/mod.py", EdgeKind.CONTAINS.value, "pkg/top_level") in edges

    # Intra-repo import resolved to a package id.
    assert ("pkg", EdgeKind.IMPORT.value, "pkg") not in edges  # no self-import


def test_python_import_and_hierarchy(tmp_path):
    # Sibling packages: b imports a (no parent/child cycle). Both under app/.
    _write(tmp_path, "app/a/mod.py", "def helper():\n    pass\n")
    _write(tmp_path, "app/b/mod.py", "from app.a import mod\n\ndef run():\n    pass\n")

    graph = build_code_graph(str(tmp_path), ["python"])
    edges = _edge_set(graph)

    # app/b imports app/a.
    assert ("app/b", EdgeKind.IMPORT.value, "app/a") in edges
    # Namespace hierarchy: app is the derived parent of app/a and app/b.
    assert ("app", EdgeKind.SUBPACKAGE.value, "app/a") in edges
    assert ("app", EdgeKind.SUBPACKAGE.value, "app/b") in edges
    # Import dependency-first: app/a (imported) before app/b (importer).
    assert graph.topo_order.index("app/a") < graph.topo_order.index("app/b")


def test_python_parent_child_import_is_a_cycle(tmp_path):
    # A child importing its own namespace parent forms a cycle (SubPackage reverse
    # edge + Import edge), faithfully reported in graph.cycles (code2skill parity).
    _write(tmp_path, "app/core/engine.py", "from app import util\n\ndef run():\n    pass\n")
    _write(tmp_path, "app/util.py", "def helper():\n    pass\n")

    graph = build_code_graph(str(tmp_path), ["python"])
    edges = _edge_set(graph)
    assert ("app/core", EdgeKind.IMPORT.value, "app") in edges
    cyclic = {pid for grp in graph.cycles for pid in grp}
    assert {"app", "app/core"} & cyclic


# ── Go extraction ─────────────────────────────────────────────────────────────────

def test_go_extraction_golden(tmp_path):
    _write(tmp_path, "go.mod", "module example.com/proj\n\ngo 1.21\n")
    _write(
        tmp_path,
        "svc/handler.go",
        '''package svc

import "example.com/proj/store"

// Serve handles requests.
func Serve(n int) int { return n }

type Server struct{ Port int }

func (s *Server) Start() {}

type Runner interface{ Run() }
''',
    )
    _write(tmp_path, "store/store.go", "package store\n\nfunc Save() {}\n")

    graph = build_code_graph(str(tmp_path), ["go"])
    by_id = _by_id(graph)

    assert by_id["svc/Serve"].kind is NodeKind.FUNCTION
    assert by_id["svc/Server"].kind is NodeKind.STRUCT
    assert by_id["svc/Runner"].kind is NodeKind.INTERFACE
    assert by_id["svc/Server.Start"].kind is NodeKind.METHOD  # receiver type prefix
    assert by_id["svc/Serve"].doc == "// Serve handles requests."
    assert by_id["svc/Serve"].signature.startswith("func Serve(n int) int")

    edges = _edge_set(graph)
    assert ("svc", EdgeKind.CONTAINS.value, "svc/handler.go") in edges
    # Import resolved via go.mod module prefix to in-repo package id.
    assert ("svc", EdgeKind.IMPORT.value, "store") in edges


def test_go_skips_test_files(tmp_path):
    _write(tmp_path, "go.mod", "module m\n")
    _write(tmp_path, "p/a.go", "package p\nfunc A() {}\n")
    _write(tmp_path, "p/a_test.go", "package p\nfunc TestA() {}\n")

    graph = build_code_graph(str(tmp_path), ["go"])
    ids = set(_by_id(graph))
    assert "p/A" in ids
    assert "p/TestA" not in ids  # _test.go excluded


# ── CodeGraph JSON round-trip ─────────────────────────────────────────────────────

def test_codegraph_json_roundtrip(tmp_path):
    _write(tmp_path, "m.py", "def f():\n    pass\n")
    graph = build_code_graph(str(tmp_path), ["python"])
    restored = from_json(to_json(graph))
    assert restored.repo_id == graph.repo_id
    assert len(restored.nodes) == len(graph.nodes)
    assert _edge_set(restored) == _edge_set(graph)
    assert restored.topo_order == graph.topo_order


# ── Projection onto ontology three tables (in-memory SQLite) ──────────────────────

def _sqlite_session_factory():
    from sqlalchemy import BigInteger, create_engine
    from sqlalchemy.dialects.mysql import LONGTEXT
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from ame_kb.models import Base

    if not getattr(_sqlite_session_factory, "_patched", False):
        @compiles(LONGTEXT, "sqlite")
        def _lt(el, comp, **kw):  # noqa: ANN001
            return "TEXT"

        @compiles(BigInteger, "sqlite")
        def _bi(el, comp, **kw):  # noqa: ANN001
            return "INTEGER"

        _sqlite_session_factory._patched = True

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _scope_factory(Session):
    @contextmanager
    def _scope():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _scope


def test_project_code_graph(tmp_path, monkeypatch):
    from ame_kb.code import project as project_mod
    from ame_kb.models import DomainEntity, GraphEdge, GraphNode
    from ame_kb.searchbackend.mysql import MysqlHybridIndex

    _write(tmp_path, "go.mod", "module m\n")
    _write(tmp_path, "svc/h.go", 'package svc\nimport "m/store"\nfunc Serve() {}\n')
    _write(tmp_path, "store/s.go", "package store\nfunc Save() {}\n")
    graph = build_code_graph(str(tmp_path), ["go"])

    Session = _sqlite_session_factory()
    monkeypatch.setattr(project_mod, "session_scope", _scope_factory(Session))
    # Search-index writes must land in the same SQLite session.
    monkeypatch.setattr(
        "ame_kb.searchindex.get_index", lambda: MysqlHybridIndex()
    )

    stats = project_mod.project_code_graph(graph)
    assert stats.nodes_new > 0
    assert stats.edges_new > 0

    with Session() as s:
        nodes = s.query(GraphNode).all()
        domain = s.query(DomainEntity).all()
        edges = s.query(GraphEdge).all()

    # Symbols + packages land in the Asset:Implementation namespace; files do not.
    node_nos = {n.graph_node_no for n in nodes}
    assert all(nn.startswith("Asset:Implementation:") for nn in node_nos)
    assert any("serve" in nn.lower() for nn in node_nos)
    # Every domain row is Asset/Implementation.
    assert {(d.type, d.entity_spec) for d in domain} == {("Asset", "Implementation")}
    # Code edges are EXTRACTED.
    assert all(e.properties.get("confidence") == "EXTRACTED" for e in edges)
    labels = {e.name for e in edges}
    assert "imports" in labels
    assert "contains" in labels  # synthesised package -> symbol
