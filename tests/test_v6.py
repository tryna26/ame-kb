"""Offline V6 tests: graph registry version resolution (incl. fresh-DB
fallback), file-manifest doc_no normalization, classify bucketing, the version
projection (_project) copy semantics, and the end-to-end build_next_version
orchestration (LLM stubbed, in-memory SQLite). No real DB/LLM/embedding calls.
"""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

import ame_kb.graphs as graphs_mod
import ame_kb.manifest as manifest_mod
import ame_kb.versioning as versioning_mod
from ame_kb.models import (
    Doc,
    DocChunk,
    DocLine,
    DomainEntity,
    EntityAlias,
    Graph,
    GraphEdge,
    GraphNode,
    SearchIndex,
)
from ame_kb.searchindex import DOC_CHUNK, EDGE, NODE
from ame_kb.versioning import Classification, classify


# ---- classify bucketing (pure) ----

def test_classify_buckets():
    manifest = {"a": "h1", "b": "h2_new", "c": "h3"}  # a unchanged, b changed, c new
    base = {"a": "h1", "b": "h2_old", "d": "h4"}  # d removed
    c = classify(manifest, base)
    assert c.unchanged == ["a"]
    assert c.changed == ["b"]
    assert c.new == ["c"]
    assert c.removed == ["d"]
    assert c.to_extract == ["b", "c"]
    assert c.has_changes


def test_classify_no_changes():
    c = classify({"a": "h1"}, {"a": "h1"})
    assert c.unchanged == ["a"]
    assert not c.has_changes


# ---- doc_no normalization (manifest) ----

def test_doc_no_relative_to_root(tmp_path):
    root = tmp_path / "kb"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "a.md"
    f.write_text("x", encoding="utf-8")
    # Same file, added via directory or directly -> identical doc_no.
    assert manifest_mod.doc_no_for(f, root) == "sub/a.md"


def test_doc_no_outside_root_falls_back_to_abs(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    other = tmp_path / "elsewhere.md"
    other.write_text("x", encoding="utf-8")
    assert manifest_mod.doc_no_for(other, root) == str(other.resolve())


# ---- version resolution is fault tolerant on a fresh DB (fix #1) ----

def test_apply_graph_context_fresh_db_no_crash(monkeypatch):
    # kg_graph missing -> latest lookup raises -> must swallow and not set version.
    def _boom():
        raise RuntimeError("no such table: kg_graph")

    monkeypatch.setattr(graphs_mod, "session_scope", _boom)
    cleared = {"n": 0}
    monkeypatch.setattr(
        graphs_mod.get_settings, "cache_clear", lambda: cleared.__setitem__("n", cleared["n"] + 1)
    )
    monkeypatch.delenv("GRAPH_VERSION", raising=False)
    monkeypatch.setenv("GRAPH_NO", "graph_abc")
    # Named graph, no explicit version -> would look up latest, but DB is down.
    graphs_mod.apply_graph_context("graph_abc", None)
    assert cleared["n"] == 1  # settings still refreshed
    # No version was forced (swallowed), so env stays unset.
    import os

    assert os.environ.get("GRAPH_VERSION") is None


def test_apply_graph_context_default_skips_lookup(monkeypatch):
    called = {"n": 0}

    def _tracker(graph_no):  # would be hit only if we tried to resolve a version
        called["n"] += 1
        raise AssertionError("default graph must not query kg_graph")

    monkeypatch.setattr(graphs_mod, "_latest_version_safe", _tracker)
    monkeypatch.setattr(graphs_mod.get_settings, "cache_clear", lambda: None)
    monkeypatch.delenv("GRAPH_NO", raising=False)
    graphs_mod.apply_graph_context(None, None)  # default graph, no version
    assert called["n"] == 0


# ---- _project + build_next_version against in-memory SQLite ----

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


def _use_mysql_index(monkeypatch):
    """Point versioning's index at the SQL backend so copy_version writes rows
    into the test's SQLite session (default backend depends on env/.env)."""
    from ame_kb.searchbackend.mysql import MysqlHybridIndex

    monkeypatch.setattr(versioning_mod, "get_index", lambda: MysqlHybridIndex())


def _seed_v1(Session):
    """v1: two docs (d1, d2). Node n_shared referenced by both; n_only1 by d1;
    n_only2 by d2. Edge d1<->shared, and search-index rows with embeddings."""
    with Session() as s:
        s.add(Graph(graph_no="g1", graph_version=1, name="G1", status="ACTIVE"))
        for doc_no in ("d1", "d2"):
            s.add(Doc(graph_no="g1", graph_version=1, doc_no=doc_no, path=doc_no,
                      title=doc_no, sha256="h_" + doc_no, source_type="md"))
            s.add(DocLine(graph_no="g1", graph_version=1, doc_no=doc_no,
                          line_no=1, content=doc_no + " line"))
            s.add(DocChunk(graph_no="g1", graph_version=1, doc_no=doc_no,
                           chunk_no="chunk_" + doc_no, chunk_index=0,
                           content=doc_no + " chunk", sha256="h_" + doc_no))
        s.add_all([
            GraphNode(graph_no="g1", graph_version=1, graph_node_no="N:shared",
                      name="Shared", type="Concept", properties={},
                      ref={"d1": ["1"], "d2": ["1"]}),
            GraphNode(graph_no="g1", graph_version=1, graph_node_no="N:only1",
                      name="Only1", type="Concept", properties={}, ref={"d1": ["1"]}),
            GraphNode(graph_no="g1", graph_version=1, graph_node_no="N:only2",
                      name="Only2", type="Concept", properties={}, ref={"d2": ["1"]}),
        ])
        s.add(GraphEdge(graph_no="g1", graph_version=1, graph_edge_no="E:1",
                        source_node_no="N:only1", target_node_no="N:shared",
                        name="related_to", properties={}, ref={"d1": ["1"]}))
        # search-index rows w/ embeddings for nodes/edges/chunks.
        for no in ("N:shared", "N:only1", "N:only2"):
            s.add(SearchIndex(graph_no="g1", graph_version=1, object_type=NODE,
                              object_no=no, searchable_text=no, embedding=[0.1, 0.2]))
        s.add(SearchIndex(graph_no="g1", graph_version=1, object_type=EDGE,
                          object_no="E:1", searchable_text="rel", embedding=[0.3]))
        for doc_no in ("d1", "d2"):
            s.add(SearchIndex(graph_no="g1", graph_version=1, object_type=DOC_CHUNK,
                              object_no="chunk_" + doc_no, searchable_text="c",
                              embedding=[0.4]))
        s.add(DomainEntity(graph_no="g1", graph_version=1, entity_name="Concept",
                           entity_type="Node", core_schema="[]"))
        s.add(EntityAlias(graph_no="g1", graph_version=1,
                          canonical_node_no="N:shared", alias="共享"))
        s.commit()


def test_project_shares_and_drops(monkeypatch):
    # Removing d2 (unchanged set = {d1}): N:shared survives (ref keeps d1),
    # N:only1 survives, N:only2 dropped (ref only d2). Edge survives (both ends
    # kept + ref d1). Alias survives (points at kept node). Index rows copied
    # verbatim (embeddings preserved -> not re-embedded).
    Session = _sqlite_session_factory()
    _seed_v1(Session)
    monkeypatch.setattr(versioning_mod, "session_scope", _scope_factory(Session))
    _use_mysql_index(monkeypatch)

    with Session() as s:
        pn, pe = versioning_mod._project(s, "g1", 1, 2, {"d1"})
        s.commit()
    assert (pn, pe) == (2, 1)

    with Session() as s:
        nodes = {n.graph_node_no for n in s.execute(
            select(GraphNode).where(GraphNode.graph_version == 2)).scalars()}
        assert nodes == {"N:shared", "N:only1"}
        shared = s.execute(select(GraphNode).where(
            GraphNode.graph_version == 2,
            GraphNode.graph_node_no == "N:shared")).scalar_one()
        assert shared.ref == {"d1": ["1"]}  # d2 filtered out
        edges = {e.graph_edge_no for e in s.execute(
            select(GraphEdge).where(GraphEdge.graph_version == 2)).scalars()}
        assert edges == {"E:1"}
        # index rows copied with embeddings intact
        idx = {(r.object_type, r.object_no): r.embedding for r in s.execute(
            select(SearchIndex).where(SearchIndex.graph_version == 2)).scalars()}
        assert idx[(NODE, "N:shared")] == [0.1, 0.2]
        assert (NODE, "N:only2") not in idx  # dropped node not indexed
        assert idx[(DOC_CHUNK, "chunk_d1")] == [0.4]
        assert (DOC_CHUNK, "chunk_d2") not in idx  # only d1 copied
        # docs/lines/chunks copied for d1 only
        docs = {d.doc_no for d in s.execute(
            select(Doc).where(Doc.graph_version == 2)).scalars()}
        assert docs == {"d1"}
        # schema layer + surviving alias copied
        assert s.execute(select(DomainEntity).where(
            DomainEntity.graph_version == 2)).scalars().first() is not None
        assert s.execute(select(EntityAlias).where(
            EntityAlias.graph_version == 2)).scalar_one().alias == "共享"


def _stub_extract(monkeypatch, calls):
    """extract() records which doc_ids it was asked to process; store/persist
    become no-ops so we test orchestration, not persistence."""
    from ame_kb.extract import ExtractionResult

    def _fake_extract(doc):
        calls.append(doc.doc_id)
        return ExtractionResult(doc_id=doc.doc_id)

    monkeypatch.setattr(versioning_mod.extract_mod, "extract", _fake_extract)
    monkeypatch.setattr(versioning_mod.store_mod, "store", lambda res: None)
    monkeypatch.setattr(versioning_mod.ingest_mod, "persist_doc", lambda doc: None)
    monkeypatch.setattr(versioning_mod.store_mod, "is_unchanged", lambda doc: False)


def _mf(doc_no, path, text, tmp_path):
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return manifest_mod.ManifestFile(doc_no=doc_no, path=str(p),
                                     source_type="md", origin_url="")


def test_build_only_extracts_changed_and_new(monkeypatch, tmp_path):
    # v1 has d1(h_d1) via _seed_v1. Manifest: d1 unchanged, d2 changed, d3 new.
    # Only d2 + d3 should hit extract; d1 inherited by projection.
    Session = _sqlite_session_factory()
    _seed_v1(Session)
    monkeypatch.setattr(versioning_mod, "session_scope", _scope_factory(Session))
    _use_mysql_index(monkeypatch)

    # Make d1's on-disk hash match the stored sha256 "h_d1".
    monkeypatch.setattr(versioning_mod.store_mod, "content_hash",
                        lambda text: "h_" + text.strip())
    calls = []
    _stub_extract(monkeypatch, calls)
    # after set_version, extraction path also calls these module funcs:
    monkeypatch.setattr(versioning_mod, "_set_version", lambda g, v: None)

    files = [
        _mf("d1", "d1.md", "d1", tmp_path),   # hash h_d1 == stored -> unchanged
        _mf("d2", "d2.md", "d2x", tmp_path),  # hash h_d2x != h_d2  -> changed
        _mf("d3", "d3.md", "d3", tmp_path),   # not in base         -> new
    ]
    res = versioning_mod.build_next_version("g1", files)
    assert res.base_version == 1 and res.target_version == 2
    assert sorted(calls) == ["d2", "d3"]  # d1 NOT re-extracted
    assert res.projected_nodes >= 1
    with Session() as s:
        v2 = s.execute(select(Graph).where(Graph.graph_version == 2)).scalar_one()
        v1 = s.execute(select(Graph).where(Graph.graph_version == 1)).scalar_one()
        assert v2.status == "ACTIVE"
        assert v1.status == "FROZEN"


def test_build_skips_when_no_changes(monkeypatch, tmp_path):
    Session = _sqlite_session_factory()
    _seed_v1(Session)
    monkeypatch.setattr(versioning_mod, "session_scope", _scope_factory(Session))
    monkeypatch.setattr(versioning_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(versioning_mod.store_mod, "content_hash",
                        lambda text: "h_" + text.strip())
    monkeypatch.setattr(versioning_mod, "_set_version", lambda g, v: None)
    calls = []
    _stub_extract(monkeypatch, calls)

    files = [
        _mf("d1", "d1.md", "d1", tmp_path),
        _mf("d2", "d2.md", "d2", tmp_path),  # both unchanged
    ]
    res = versioning_mod.build_next_version("g1", files)
    assert res.skipped is True
    assert calls == []
    with Session() as s:
        # No v2 created.
        assert s.execute(select(Graph).where(Graph.graph_version == 2)).scalar_one_or_none() is None


class _Settings:
    graph_no = "g1"
    graph_version = 1
    source_dir = "."
    schema_dynamic = False
