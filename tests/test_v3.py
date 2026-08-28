"""Offline V3 regression and SQLite integration tests.

External LLM and embedding calls are mocked.  The SQLite cases exercise the
same SQLAlchemy transaction boundaries used by the production store, resolver,
query layer, and identity migration.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ame_kb import embed as embed_mod
from ame_kb import cli as cli_mod
from ame_kb import migrations as migrations_mod
from ame_kb import query as query_mod
from ame_kb import resolve as resolve_mod
from ame_kb import store as store_mod
from ame_kb.extract import ExtractionResult, validate
from ame_kb.ingest import Document
from ame_kb.migrations import MigrationStats, ensure_v3_schema, migrate_identity
from ame_kb.models import (
    Base,
    DocVersion,
    EdgeContribution,
    GraphEdge,
    GraphNode,
    LegacyNodeId,
    MergeLog,
    NodeAlias,
    NodeContribution,
)
from ame_kb.vecmath import cosine, validate_vector


SCOPE = {"graph_no": "g", "graph_version": 3}
T0 = datetime(2026, 1, 1)
T1 = datetime(2026, 1, 2)


def test_validate_uses_mention_ids_for_same_name_across_types():
    payload = {
        "nodes": [
            {"mention_id": "person", "name": "Atlas", "type": "Person"},
            {"mention_id": "project", "name": "Atlas", "type": "Project"},
        ],
        "edges": [
            {
                "source_mention_id": "person",
                "target_mention_id": "project",
                "label": "part_of",
            },
            {
                "source_name": "Atlas",
                "target_mention_id": "project",
                "label": "part_of",
            },
            {
                "source_mention_id": "missing",
                "target_mention_id": "project",
                "label": "part_of",
            },
        ],
    }

    result = validate("doc", payload)

    assert {(node.mention_id, node.type) for node in result.nodes} == {
        ("person", "Person"),
        ("project", "Project"),
    }
    assert len(result.edges) == 1
    assert result.edges[0].source_mention_id == "person"
    assert result.edges[0].target_mention_id == "project"
    assert any("ambiguous legacy name" in item for item in result.dropped)
    assert any("unknown mention_id" in item for item in result.dropped)


def test_validate_generates_stable_ids_and_drops_duplicate_mentions():
    single = {"nodes": [{"name": "  Ada  Lovelace ", "type": "Person"}]}
    first = validate("d1", single)
    second = validate("d2", single)
    duplicate = validate(
        "d3",
        {
            "nodes": [
                {"mention_id": "a", "name": "Ada", "type": "Person"},
                {"mention_id": "b", "name": " ADA ", "type": "Person"},
            ]
        },
    )

    assert first.nodes[0].mention_id == second.nodes[0].mention_id
    assert first.nodes[0].mention_id.startswith("m-")
    assert duplicate.nodes == []
    assert len(duplicate.dropped) == 2
    assert all("ambiguous duplicate" in item for item in duplicate.dropped)


@pytest.mark.parametrize(
    "vector, dimension",
    [
        ([], None),
        ([1.0], 2),
        ([True], None),
        (["1"], None),
        ([float("nan")], None),
        ([float("inf")], None),
    ],
)
def test_vector_validation_rejects_malformed_embeddings(vector, dimension):
    with pytest.raises(ValueError):
        validate_vector(vector, dimension=dimension)


def test_cosine_normalizes_and_rejects_invalid_vectors():
    assert cosine([3.0, 0.0], [9.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-2.0, 0.0]) == pytest.approx(-1.0)
    for left, right in [([], [1.0]), ([1.0], [1.0, 2.0]), ([0.0], [1.0])]:
        with pytest.raises(ValueError):
            cosine(left, right)


def test_embedding_batch_validation_reorders_and_rejects_provider_bool():
    rows = [
        SimpleNamespace(index=1, embedding=[0.0, 1.0]),
        SimpleNamespace(index=0, embedding=[1.0, 0.0]),
    ]

    assert embed_mod._validate_batch(rows, 2, expected_dimension=2) == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]
    with pytest.raises(ValueError, match="not a number"):
        embed_mod._validate_batch(
            [SimpleNamespace(index=0, embedding=[True, 0.0])],
            1,
            expected_dimension=2,
        )


def test_embed_texts_uses_mocked_endpoint_and_validates_dimension(monkeypatch):
    calls = []

    class Embeddings:
        def create(self, *, model, input):
            calls.append((model, list(input)))
            data = [
                SimpleNamespace(index=index, embedding=[float(index), 1.0])
                for index, _value in reversed(list(enumerate(input)))
            ]
            return SimpleNamespace(data=data)

    settings = SimpleNamespace(
        llm_api_key="llm-key",
        llm_base_url="https://llm.invalid",
        embed_api_key="embed-key",
        embed_base_url="https://embed.invalid",
        embed_model="embed-v3",
        embed_dim=2,
    )
    monkeypatch.setattr(embed_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(
        embed_mod, "_client", lambda: SimpleNamespace(embeddings=Embeddings())
    )

    vectors = embed_mod.embed_texts(["a", "b", "c"], batch_size=2)

    assert vectors == [[0.0, 1.0], [1.0, 1.0], [0.0, 1.0]]
    assert calls == [("embed-v3", ["a", "b"]), ("embed-v3", ["c"])]
    assert embed_mod.embedding_hash("Ada", "m1") != embed_mod.embedding_hash(
        "Ada", "m2"
    )
    assert "名字" in embed_mod.embedding_text(
        SimpleNamespace(
            name="Ada", type="Person", description="数学家", properties={"名字": "阿达"}
        )
    )


@pytest.fixture
def v3_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, future=True, expire_on_commit=False
    )

    @contextmanager
    def scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    settings = SimpleNamespace(
        graph_no="g",
        graph_version=3,
        source_id="src",
        resolve_low_threshold=0.75,
        resolve_high_threshold=0.92,
        resolve_candidate_topk=10,
        embed_model="embed-v3",
        embed_dim=2,
    )
    monkeypatch.setattr(resolve_mod, "session_scope", scope)
    monkeypatch.setattr(query_mod, "session_scope", scope)
    monkeypatch.setattr(store_mod, "session_scope", scope)
    monkeypatch.setattr(resolve_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(query_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(store_mod, "get_settings", lambda: settings)
    return SimpleNamespace(
        engine=engine,
        Session=session_factory,
        scope=scope,
        settings=settings,
    )


def _node(node_no, name, type_="Organization", *, created=T0, **kwargs):
    values = {
        "description": None,
        "properties": {},
        "ref": {},
        "aliases": [],
        "merged_into": None,
        "embedding": None,
        "embedding_hash": None,
        "embedding_model": None,
        "deleted": 0,
        "create_time": created,
        "update_time": created,
    }
    values.update(kwargs)
    return GraphNode(
        **SCOPE,
        graph_node_no=node_no,
        name=name,
        type=type_,
        **values,
    )


def _edge(edge_no, source, label, target, *, confidence="INFERRED", **kwargs):
    values = {
        "description": None,
        "properties": {"confidence": confidence},
        "ref": {},
        "deleted": 0,
        "create_time": T0,
        "update_time": T0,
    }
    values.update(kwargs)
    return GraphEdge(
        **SCOPE,
        graph_edge_no=edge_no,
        source_node_no=source,
        target_node_no=target,
        name=label,
        **values,
    )


def _seed_merge_graph(db):
    with db.Session.begin() as session:
        session.add_all(
            [
                _node(
                    "W",
                    "Acme",
                    properties={
                        "keep": "winner",
                        "nested": {"keep": 1, "gap": ""},
                    },
                    ref={"docs": ["w"], "meta": {"winner": 1}},
                    aliases=["ACME", "A"],
                    embedding=[1.0, 0.0],
                    embedding_hash="w" * 64,
                    embedding_model="old",
                ),
                _node(
                    "L",
                    "Acme Corporation",
                    created=T1,
                    properties={
                        "keep": "loser",
                        "nested": {"gap": "filled", "added": 2},
                    },
                    ref={"docs": ["l", "w"], "meta": {"loser": 2}},
                    aliases=["Acme Corp", "A"],
                ),
                _node("X", "Target"),
                _node("Y", "Caller"),
            ]
        )
        session.flush()
        session.add_all(
            [
                NodeAlias(
                    **SCOPE,
                    type="Organization",
                    normalized_alias=resolve_mod.normalize_alias("ACME"),
                    canonical_node_no="W",
                    alias="ACME",
                    source="seed",
                ),
                NodeAlias(
                    **SCOPE,
                    type="Organization",
                    normalized_alias=resolve_mod.normalize_alias("Acme Corp"),
                    canonical_node_no="L",
                    alias="Acme Corp",
                    source="seed",
                ),
                _edge(
                    resolve_mod._edge_no("W", "owns", "X"),
                    "W",
                    "owns",
                    "X",
                    confidence="AMBIGUOUS",
                    description="short",
                    properties={"confidence": "AMBIGUOUS", "winner": 1},
                    ref={"docs": ["w"]},
                ),
                _edge(
                    "legacy-duplicate",
                    "L",
                    "owns",
                    "X",
                    confidence="EXTRACTED",
                    description="a much longer description",
                    properties={"confidence": "EXTRACTED", "loser": 2},
                    ref={"docs": ["l"]},
                ),
                _edge("legacy-loop", "L", "related_to", "W"),
                _edge("legacy-remap", "Y", "related_to", "L"),
                NodeContribution(
                    **SCOPE,
                    source_id="src",
                    doc_id="loser.md",
                    doc_key="d" * 64,
                    mention_key="m" * 64,
                    canonical_node_no="L",
                    name="Acme Corporation",
                    type="Organization",
                    properties={"loser": 1},
                    ref={"loser.md": ["1"]},
                    extraction_hash="x" * 64,
                ),
                EdgeContribution(
                    **SCOPE,
                    source_id="src",
                    doc_id="edge.md",
                    doc_key="e" * 64,
                    edge_key="k" * 64,
                    source_mention_key="s" * 64,
                    target_mention_key="t" * 64,
                    source_node_no="L",
                    target_node_no="W",
                    name="related_to",
                    confidence="EXTRACTED",
                    properties={},
                    ref={"edge.md": ["2"]},
                    extraction_hash="h" * 64,
                ),
            ]
        )


@pytest.mark.parametrize("pair", [("W", "L"), ("L", "W")])
def test_merge_full_contract_and_deterministic_survivor(v3_db, pair):
    _seed_merge_graph(v3_db)

    merge_id = resolve_mod.merge(
        *pair, canonical_name="Acme Corporation", reason="same company"
    )

    with v3_db.Session() as session:
        winner = session.scalar(select(GraphNode).where(GraphNode.graph_node_no == "W"))
        loser = session.scalar(select(GraphNode).where(GraphNode.graph_node_no == "L"))
        assert winner.name == "Acme Corporation"
        assert winner.properties == {
            "keep": "winner",
            "nested": {"keep": 1, "gap": "filled", "added": 2},
        }
        assert winner.ref == {
            "docs": ["l", "w"],
            "meta": {"winner": 1, "loser": 2},
        }
        assert set(winner.aliases) == {
            "Acme Corporation",
            "Acme",
            "ACME",
            "A",
            "Acme Corp",
        }
        assert winner.embedding is None
        assert winner.embedding_hash is None
        assert winner.embedding_model is None
        assert loser.deleted == 1
        assert loser.merged_into == "W"

        aliases = session.scalars(select(NodeAlias)).all()
        assert aliases
        assert {row.canonical_node_no for row in aliases} == {"W"}
        assert all(row.source == "merge" for row in aliases)

        duplicate_group = session.scalars(
            select(GraphEdge).where(GraphEdge.name == "owns")
        ).all()
        live = [edge for edge in duplicate_group if edge.deleted == 0]
        assert len(live) == 1
        assert (live[0].source_node_no, live[0].target_node_no) == ("W", "X")
        assert live[0].properties == {
            "confidence": "EXTRACTED",
            "winner": 1,
            "loser": 2,
        }
        assert live[0].ref == {"docs": ["l", "w"]}
        assert live[0].description == "a much longer description"
        assert session.scalar(
            select(GraphEdge).where(GraphEdge.graph_edge_no.like("tmp:%"))
        ) is not None
        remapped = session.scalar(
            select(GraphEdge).where(
                GraphEdge.source_node_no == "Y",
                GraphEdge.target_node_no == "W",
                GraphEdge.deleted == 0,
            )
        )
        assert remapped.graph_edge_no == resolve_mod._edge_no("Y", "related_to", "W")
        node_contribution = session.scalar(select(NodeContribution))
        edge_contribution = session.scalar(select(EdgeContribution))
        assert node_contribution.canonical_node_no == "W"
        assert (edge_contribution.source_node_no, edge_contribution.target_node_no) == (
            "W",
            "W",
        )
        log = session.scalar(select(MergeLog).where(MergeLog.merge_id == merge_id))
        assert (log.winner_node_no, log.loser_node_no, log.status) == (
            "W",
            "L",
            "MERGED",
        )
        assert {
            "winner",
            "loser",
            "aliases",
            "edges",
            "node_contributions",
            "edge_contributions",
            "after",
        } <= set(log.snapshot)


def _semantic_snapshot(session):
    nodes = {
        row.graph_node_no: (
            row.name,
            row.description,
            row.properties,
            row.ref,
            row.aliases,
            row.merged_into,
            row.deleted,
            row.embedding,
            row.embedding_hash,
            row.embedding_model,
        )
        for row in session.scalars(select(GraphNode)).all()
    }
    edges = {
        row.id: (
            row.graph_edge_no,
            row.source_node_no,
            row.target_node_no,
            row.name,
            row.description,
            row.properties,
            row.ref,
            row.deleted,
        )
        for row in session.scalars(select(GraphEdge)).all()
    }
    aliases = {
        (row.type, row.normalized_alias, row.canonical_node_no, row.alias, row.source)
        for row in session.scalars(select(NodeAlias)).all()
    }
    node_contributions = {
        row.id: row.canonical_node_no
        for row in session.scalars(select(NodeContribution)).all()
    }
    edge_contributions = {
        row.id: (row.source_node_no, row.target_node_no)
        for row in session.scalars(select(EdgeContribution)).all()
    }
    return nodes, edges, aliases, node_contributions, edge_contributions


def test_merge_rollback_restores_complete_before_image(v3_db):
    _seed_merge_graph(v3_db)
    with v3_db.Session() as session:
        before = _semantic_snapshot(session)

    merge_id = resolve_mod.merge("L", "W", canonical_name="Acme Corporation")
    resolve_mod.rollback(merge_id)

    with v3_db.Session() as session:
        assert _semantic_snapshot(session) == before
        assert session.scalar(
            select(MergeLog).where(MergeLog.merge_id == merge_id)
        ).status == "ROLLED_BACK"
    with pytest.raises(ValueError, match="not active"):
        resolve_mod.rollback(merge_id)


@pytest.mark.parametrize(
    "case, score, decision, expected_bucket",
    [
        ("high", 0.92, None, "high_merged"),
        ("grey-same", 0.75, "same", "llm_merged"),
        ("grey-related", 0.80, "related", None),
        ("low", 0.749999, None, None),
    ],
)
def test_resolve_all_vector_gate_matrix(
    v3_db, monkeypatch, case, score, decision, expected_bucket
):
    with v3_db.Session.begin() as session:
        session.add_all([_node("A", "Alpha"), _node("B", "Beta", created=T1)])

    def candidates(session, node, limit=None):
        other = session.scalars(
            select(GraphNode)
            .where(
                GraphNode.deleted == 0,
                GraphNode.merged_into.is_(None),
                GraphNode.type == node.type,
                GraphNode.graph_node_no != node.graph_node_no,
            )
            .order_by(GraphNode.graph_node_no)
        ).first()
        return [] if other is None else [(other, score)]

    judge_calls = []

    def fake_judge(left, right):
        judge_calls.append((left.graph_node_no, right.graph_node_no))
        return resolve_mod.JudgeResult(decision, "Beta" if decision == "same" else "", "mock")

    monkeypatch.setattr(resolve_mod, "find_candidates", candidates)
    monkeypatch.setattr(resolve_mod, "judge", fake_judge)

    stats = resolve_mod.resolve_all()

    assert stats.pairs == 1
    assert stats.merged == (1 if expected_bucket else 0)
    if expected_bucket:
        assert getattr(stats, expected_bucket) == 1
        second = resolve_mod.resolve_all()
        assert second.merged == 0
        assert second.pairs == 0
        with v3_db.Session() as session:
            assert session.query(MergeLog).count() == 1
    else:
        assert stats.skipped == 1
    assert bool(judge_calls) == case.startswith("grey")


def test_resolve_all_exact_and_cross_type_blocking(v3_db, monkeypatch):
    with v3_db.Session.begin() as session:
        session.add_all(
            [
                _node("A", "Acme"),
                _node("B", "ＡＣＭＥ", created=T1),
                _node("P", "Acme", type_="Person", created=T1),
            ]
        )

    def no_vector(*args, **kwargs):
        raise AssertionError("exact matches must bypass vector recall")

    def no_judge(*args, **kwargs):
        raise AssertionError("exact matches must bypass the LLM judge")

    monkeypatch.setattr(resolve_mod, "find_candidates", no_vector)
    monkeypatch.setattr(resolve_mod, "judge", no_judge)

    stats = resolve_mod.resolve_all(type_filter="Organization")

    assert stats.exact_merged == 1
    assert stats.merged == 1
    with v3_db.Session() as session:
        person = session.scalar(select(GraphNode).where(GraphNode.graph_node_no == "P"))
        assert person.deleted == 0
        assert person.merged_into is None


def test_query_alias_legacy_loser_redirect_and_cycle_guard(v3_db):
    with v3_db.Session.begin() as session:
        session.add_all(
            [
                _node("W", "Acme Corporation"),
                _node("L", "Acme", deleted=1, merged_into="W", created=T1),
                _node("X", "Target"),
                _node("C1", "Cycle One", deleted=1, merged_into="C2"),
                _node("C2", "Cycle Two", deleted=1, merged_into="C1"),
                NodeAlias(
                    **SCOPE,
                    type="Organization",
                    normalized_alias="acme",
                    canonical_node_no="L",
                    alias="ACME",
                    source="seed",
                ),
                LegacyNodeId(
                    **SCOPE, legacy_node_no="legacy-acme", canonical_node_no="L"
                ),
                _edge(
                    resolve_mod._edge_no("W", "owns", "X"),
                    "W",
                    "owns",
                    "X",
                ),
            ]
        )

    with v3_db.Session() as session:
        assert query_mod.resolve_node_identifier(session, "W").graph_node_no == "W"
        assert query_mod.resolve_node_identifier(session, "L").graph_node_no == "W"
        assert (
            query_mod.resolve_node_identifier(session, "legacy-acme").graph_node_no
            == "W"
        )
        assert query_mod.resolve_node_identifier(session, "missing") is None
        with pytest.raises(ValueError, match="cycle"):
            query_mod.resolve_node_identifier(session, "C1")

    assert [hit.graph_node_no for hit in query_mod.find_entities("ACME")] == ["W"]
    assert [
        hit.graph_node_no
        for hit in query_mod.find_entities_exact("Organization", "ACME")
    ] == ["W"]
    assert query_mod.find_entities_exact("Person", "ACME") == []
    expected = [("out", "owns", "X")]
    assert [
        (hit.direction, hit.label, hit.other_no)
        for hit in query_mod.relations_of("legacy-acme")
    ] == expected
    assert [
        (hit.direction, hit.label, hit.other_no)
        for hit in query_mod.relations_of("L")
    ] == expected
    with pytest.raises(ValueError, match="cycle"):
        query_mod.relations_of("C1")


def _document(doc_id, text):
    return Document(doc_id=doc_id, path=Path(doc_id), text=text)


def _validated_result(doc_id, nodes, edges=()):
    result = validate(doc_id, {"nodes": nodes, "edges": list(edges)})
    assert result.dropped == []
    return result


def test_stable_keys_normalize_mentions_but_scope_documents():
    first = migrations_mod.stable_mention_key("Person", " Ａda   Lovelace " )
    second = migrations_mod.stable_mention_key("Person", "ada lovelace")

    assert first == second
    assert len(first) == 64
    assert migrations_mod.stable_doc_key("source-a", "doc.md") != (
        migrations_mod.stable_doc_key("source-b", "doc.md")
    )
    assert migrations_mod.stable_edge_key(first, "works_for", second) == (
        migrations_mod.stable_edge_key(first, "works_for", second)
    )


def test_sql_splitter_preserves_semicolons_inside_comments_and_literals():
    body = "CREATE TABLE t (v VARCHAR(10)) COMMENT='one; two'; SELECT `a;b`;"

    assert cli_mod._split_sql_statements(body) == [
        "CREATE TABLE t (v VARCHAR(10)) COMMENT='one; two'",
        "SELECT `a;b`",
    ]


def test_store_document_force_reingest_keeps_uuid_and_replaces_old_facts(v3_db):
    doc = _document("facts.md", "version one")
    first = _validated_result(
        doc.doc_id,
        [
            {
                "mention_id": "person-v1",
                "name": "Ada",
                "type": "Person",
                "properties": {"keep": "old", "removed": True},
                "source": ["1-1"],
            },
            {
                "mention_id": "org-v1",
                "name": "ACME",
                "type": "Organization",
                "source": ["2-2"],
            },
        ],
        [
            {
                "source_mention_id": "person-v1",
                "target_mention_id": "org-v1",
                "label": "works_for",
                "confidence": "EXTRACTED",
                "source": ["3-3"],
            }
        ],
    )
    store_mod.store_document(first, doc, source_id="source")

    with v3_db.Session() as session:
        original = {row.type: row.graph_node_no for row in session.scalars(select(GraphNode))}
        assert all(value.startswith("node:") for value in original.values())

    # --force re-extracts and calls store_document again.  A document-local
    # mention must retain its durable Node UUID and must not duplicate the edge.
    store_mod.store_document(first, doc, source_id="source")
    with v3_db.Session() as session:
        forced = {row.type: row.graph_node_no for row in session.scalars(select(GraphNode))}
        assert forced == original
        assert session.query(NodeContribution).count() == 2
        assert session.query(EdgeContribution).count() == 1

        assert session.query(GraphEdge).filter(GraphEdge.deleted == 0).count() == 1

    changed_doc = _document("facts.md", "version two")
    replacement = _validated_result(
        changed_doc.doc_id,
        [
            {
                "mention_id": "person-v2",
                "name": " ADA ",
                "type": "Person",
                "properties": {"keep": "new"},
                "source": ["9-9"],
            }
        ],
    )
    store_mod.store_document(replacement, changed_doc, source_id="source")

    with v3_db.Session() as session:
        person = session.scalar(select(GraphNode).where(GraphNode.type == "Person"))
        organization = session.scalar(
            select(GraphNode).where(GraphNode.type == "Organization")
        )
        edge = session.scalar(select(GraphEdge))
        contribution = session.scalar(select(NodeContribution))
        version = session.scalar(select(DocVersion))
        assert person.graph_node_no == original["Person"]
        assert person.properties == {"keep": "new"}
        assert person.ref == {"source::facts.md": ["9-9"]}
        assert organization.deleted == 1
        assert organization.properties == {}
        assert edge.deleted == 1
        assert edge.properties == {}
        assert contribution.canonical_node_no == person.graph_node_no
        assert version.content_hash == store_mod.content_hash("version two")


def test_store_document_rename_keeps_uuid_by_locator_and_safe_fallback(v3_db):
    doc = _document("rename.md", "v1")
    first = _validated_result(
        doc.doc_id,
        [{"mention_id": "person-1", "name": "Ada", "type": "Person"}],
    )
    store_mod.store_document(first, doc, source_id="source")
    with v3_db.Session() as session:
        original_no = session.scalar(select(GraphNode.graph_node_no))

    same_locator = _validated_result(
        doc.doc_id,
        [
            {
                "mention_id": "person-1",
                "name": "Ada Lovelace",
                "type": "Person",
            }
        ],
    )
    store_mod.store_document(
        same_locator, _document(doc.doc_id, "v2"), source_id="source"
    )
    with v3_db.Session() as session:
        renamed = session.scalar(select(GraphNode).where(GraphNode.deleted == 0))
        contribution = session.scalar(select(NodeContribution))
        assert renamed.graph_node_no == original_no
        assert renamed.name == "Ada Lovelace"
        assert contribution.mention_id == "person-1"

    # If the model changes its local locator, one unmatched old/new Person is
    # still a safe one-to-one rename. Ambiguous multi-entity cases never guess.
    changed_locator = _validated_result(
        doc.doc_id,
        [
            {
                "mention_id": "model-renumbered",
                "name": "Augusta Ada King",
                "type": "Person",
            }
        ],
    )
    store_mod.store_document(
        changed_locator, _document(doc.doc_id, "v3"), source_id="source"
    )
    with v3_db.Session() as session:
        renamed = session.scalar(select(GraphNode).where(GraphNode.deleted == 0))
        assert renamed.graph_node_no == original_no
        assert renamed.name == "Augusta Ada King"
        assert {"Ada", "Ada Lovelace"} <= set(renamed.aliases)
        history = list(
            session.scalars(
                select(NodeAlias).where(
                    NodeAlias.canonical_node_no == original_no,
                    NodeAlias.source == "history",
                )
            )
        )
        assert {row.alias for row in history} == {"Ada", "Ada Lovelace"}


def test_document_recompute_preserves_migration_alias(v3_db):
    doc = _document("alias.md", "v1")
    result = _validated_result(
        doc.doc_id,
        [{"mention_id": "p", "name": "Ada Lovelace", "type": "Person"}],
    )
    store_mod.store_document(result, doc, source_id="source")
    with v3_db.Session.begin() as session:
        node = session.scalar(select(GraphNode).where(GraphNode.deleted == 0))
        session.add(
            NodeAlias(
                **SCOPE,
                type="Person",
                normalized_alias="countess",
                canonical_node_no=node.graph_node_no,
                alias="Countess",
                source="migration",
            )
        )

    store_mod.store_document(
        result, _document(doc.doc_id, "v2"), source_id="source"
    )
    with v3_db.Session() as session:
        node = session.scalar(select(GraphNode).where(GraphNode.deleted == 0))
        aliases = list(
            session.scalars(
                select(NodeAlias).where(NodeAlias.canonical_node_no == node.graph_node_no)
            )
        )
        assert "Countess" in node.aliases
        assert any(row.alias == "Countess" and row.source == "migration" for row in aliases)


def test_store_document_preserves_other_document_contribution_after_replace(v3_db):
    doc_a = _document("a.md", "a1")
    doc_b = _document("b.md", "b1")
    result_a = _validated_result(
        doc_a.doc_id,
        [
            {
                "mention_id": "a",
                "name": "Ada",
                "type": "Person",
                "properties": {"winner": 1, "shared": "a"},
                "source": ["1"],
            }
        ],
    )
    result_b = _validated_result(
        doc_b.doc_id,
        [
            {
                "mention_id": "b",
                "name": "Ada Lovelace",
                "type": "Person",
                "properties": {"remaining": 2, "shared": "b"},
                "source": ["2"],
            }
        ],
    )
    store_mod.store_document(result_a, doc_a, source_id="source")
    store_mod.store_document(result_b, doc_b, source_id="source")
    with v3_db.Session() as session:
        node_nos = [
            row.graph_node_no
            for row in session.scalars(select(GraphNode).order_by(GraphNode.id))
        ]
    resolve_mod.merge(*node_nos)

    empty = ExtractionResult(doc_id=doc_a.doc_id)
    store_mod.store_document(empty, _document(doc_a.doc_id, "a2"), source_id="source")

    with v3_db.Session() as session:
        live = session.scalars(select(GraphNode).where(GraphNode.deleted == 0)).all()
        assert len(live) == 1
        assert live[0].name == "Ada Lovelace"
        assert live[0].properties == {"remaining": 2, "shared": "b"}
        assert live[0].ref == {"source::b.md": ["2"]}
        contributions = session.scalars(select(NodeContribution)).all()
        assert len(contributions) == 1
        assert contributions[0].doc_id == "b.md"
        assert contributions[0].canonical_node_no == live[0].graph_node_no


def test_merge_survivor_precedence_survives_loser_reingest(v3_db):
    """A later re-ingest must not undo the survivor-wins merge contract."""

    winner_doc = _document("winner.md", "winner-v1")
    loser_doc = _document("loser.md", "loser-v1")
    winner_result = _validated_result(
        winner_doc.doc_id,
        [
            {
                "mention_id": "winner",
                "name": "Ada",
                "type": "Person",
                "properties": {"role": "canonical", "winner_only": 1},
            }
        ],
    )
    loser_result = _validated_result(
        loser_doc.doc_id,
        [
            {
                "mention_id": "loser",
                "name": "Ada Lovelace",
                "type": "Person",
                "properties": {"role": "loser", "loser_only": 2},
            }
        ],
    )
    store_mod.store_document(winner_result, winner_doc, source_id="source")
    store_mod.store_document(loser_result, loser_doc, source_id="source")

    with v3_db.Session() as session:
        nodes = list(session.scalars(select(GraphNode).order_by(GraphNode.id)))
        winner_no, loser_no = nodes[0].graph_node_no, nodes[1].graph_node_no
    resolve_mod.merge(winner_no, loser_no, canonical_name="Ada Lovelace")

    loser_v2 = _validated_result(
        loser_doc.doc_id,
        [
            {
                "mention_id": "new-run-local-id",
                "name": "Ada Lovelace",
                "type": "Person",
                "properties": {"role": "new-loser-value", "new_field": 3},
            }
        ],
    )
    store_mod.store_document(
        loser_v2, _document(loser_doc.doc_id, "loser-v2"), source_id="source"
    )

    with v3_db.Session() as session:
        survivor = session.scalar(
            select(GraphNode).where(
                GraphNode.graph_node_no == winner_no, GraphNode.deleted == 0
            )
        )
        contributions = list(
            session.scalars(
                select(NodeContribution)
                .where(NodeContribution.canonical_node_no == winner_no)
                .order_by(NodeContribution.canonical_rank, NodeContribution.id)
            )
        )
        assert survivor.properties == {
            "role": "canonical",
            "winner_only": 1,
            "new_field": 3,
        }
        assert [row.canonical_rank for row in contributions] == [0, 1]


def test_edge_aggregation_keeps_highest_confidence(v3_db):
    with v3_db.Session.begin() as session:
        session.add_all([_node("A", "Ada", type_="Person"), _node("B", "ACME")])
        for index, confidence in enumerate(("EXTRACTED", "INFERRED", "AMBIGUOUS")):
            session.add(
                EdgeContribution(
                    **SCOPE,
                    source_id="src",
                    doc_id=f"d{index}",
                    doc_key=f"{index + 1:064x}",
                    edge_key=f"{index + 11:064x}",
                    source_mention_key=f"{index + 21:064x}",
                    target_mention_key=f"{index + 31:064x}",
                    source_node_no="A",
                    target_node_no="B",
                    name="works_for",
                    confidence=confidence,
                    properties={"from": confidence},
                    ref={f"d{index}": [str(index)]},
                )
            )
        store_mod.recompute_edges(session, settings=v3_db.settings)

    with v3_db.Session() as session:
        edge = session.scalar(select(GraphEdge).where(GraphEdge.deleted == 0))
        assert edge.properties["confidence"] == "EXTRACTED"
        assert edge.ref == {"d0": ["0"], "d1": ["1"], "d2": ["2"]}


def test_store_document_failure_rolls_back_graph_contributions_and_hash(
    v3_db, monkeypatch
):
    doc = _document("atomic.md", "before")
    baseline = _validated_result(
        doc.doc_id,
        [
            {
                "mention_id": "a",
                "name": "Ada",
                "type": "Person",
                "properties": {"state": "before"},
            }
        ],
    )
    store_mod.store_document(baseline, doc, source_id="source")

    changed_doc = _document(doc.doc_id, "after")
    changed = _validated_result(
        doc.doc_id,
        [
            {
                "mention_id": "different-run-id",
                "name": "Ada",
                "type": "Person",
                "properties": {"state": "after"},
            }
        ],
    )

    def fail_before_doc_version(*args, **kwargs):
        raise RuntimeError("simulated document-version failure")

    monkeypatch.setattr(store_mod, "_upsert_doc_version", fail_before_doc_version)
    with pytest.raises(RuntimeError, match="simulated document-version failure"):
        store_mod.store_document(changed, changed_doc, source_id="source")

    with v3_db.Session() as session:
        node = session.scalar(select(GraphNode))
        contribution = session.scalar(select(NodeContribution))
        version = session.scalar(select(DocVersion))
        assert node.properties == {"state": "before"}
        assert contribution.properties == {"state": "before"}
        assert version.content_hash == store_mod.content_hash("before")


def test_mark_processed_rejects_stale_hash_only_update(v3_db):
    doc = _document("mark.md", "stored")
    result = _validated_result(
        doc.doc_id,
        [{"mention_id": "p", "name": "Ada", "type": "Person"}],
    )
    store_mod.store_document(result, doc, source_id="source")

    # A no-op compatibility mark remains valid.
    store_mod.mark_processed(doc, source_id="source")
    with pytest.raises(RuntimeError, match="stored contributions"):
        store_mod.mark_processed(
            _document(doc.doc_id, "new-without-contributions"),
            source_id="source",
        )

    with v3_db.Session() as session:
        version = session.scalar(select(DocVersion))
        assert version.content_hash == store_mod.content_hash("stored")


def test_mark_processed_rejects_unattributed_legacy_contribution(v3_db):
    doc = _document("legacy.md", "new text")
    with v3_db.Session.begin() as session:
        node = _node("legacy-node", "Legacy", type_="Person")
        session.add(node)
        session.flush()
        session.add(
            NodeContribution(
                **SCOPE,
                source_id="source",
                doc_id=doc.doc_id,
                doc_key=migrations_mod.stable_doc_key("source", doc.doc_id),
                mention_key=migrations_mod.stable_mention_key("Person", "Legacy"),
                mention_id=None,
                canonical_node_no=node.graph_node_no,
                canonical_rank=0,
                name=node.name,
                type=node.type,
                properties={},
                ref={"source::legacy.md": ["1"]},
                extraction_hash=None,
            )
        )

    with pytest.raises(RuntimeError, match="unattributed legacy"):
        store_mod.mark_processed(doc, source_id="source")
    with v3_db.Session() as session:
        assert session.query(DocVersion).count() == 0


def _legacy_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE kg_graph_node (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_no VARCHAR(128) NOT NULL DEFAULT 'default',
                graph_version BIGINT NOT NULL DEFAULT 1,
                graph_node_no VARCHAR(191) NOT NULL,
                name VARCHAR(255) NOT NULL DEFAULT '',
                type VARCHAR(64) NOT NULL DEFAULT '',
                properties JSON NULL, ref JSON NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uk_node_no UNIQUE (graph_no, graph_version, graph_node_no)
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE kg_graph_edge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_no VARCHAR(128) NOT NULL DEFAULT 'default',
                graph_version BIGINT NOT NULL DEFAULT 1,
                graph_edge_no VARCHAR(191) NOT NULL,
                source_node_no VARCHAR(191) NOT NULL,
                target_node_no VARCHAR(191) NOT NULL,
                name VARCHAR(64) NOT NULL DEFAULT '',
                properties JSON NULL, ref JSON NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uk_edge_no UNIQUE (graph_no, graph_version, graph_edge_no)
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE kg_doc_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_no VARCHAR(128) NOT NULL DEFAULT 'default',
                graph_version BIGINT NOT NULL DEFAULT 1,
                doc_id VARCHAR(512) NOT NULL,
                content_hash CHAR(64) NOT NULL,
                create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uk_doc UNIQUE (graph_no, graph_version, doc_id)
            )
            """
        )
        conn.execute(
            text(
                "INSERT INTO kg_graph_node "
                "(graph_no, graph_version, graph_node_no, name, type) "
                "VALUES ('g', 7, 'Person:ada', 'Ada Lovelace', 'Person')"
            )
        )
    return engine


def _schema_signature(engine):
    inspector = inspect(engine)
    return {
        table: {
            "columns": sorted(item["name"] for item in inspector.get_columns(table)),
            "indexes": sorted(item["name"] for item in inspector.get_indexes(table)),
            "uniques": sorted(
                item["name"]
                for item in inspector.get_unique_constraints(table)
                if item.get("name")
            ),
        }
        for table in sorted(inspector.get_table_names())
    }


def test_ensure_v3_schema_upgrades_legacy_schema_idempotently():
    engine = _legacy_engine()

    ensure_v3_schema(engine)
    first = _schema_signature(engine)
    ensure_v3_schema(engine)

    assert _schema_signature(engine) == first
    assert {
        "kg_graph_node",
        "kg_graph_edge",
        "kg_doc_version",
        "kg_node_alias",
        "kg_node_legacy_id",
        "kg_node_contribution",
        "kg_edge_contribution",
        "kg_merge_log",
        "kg_schema_migration",
        "kg_graph_write_lock",
    } <= set(first)
    assert "mention_id" in first["kg_node_contribution"]["columns"]
    assert "canonical_rank" in first["kg_node_contribution"]["columns"]
    assert {
        "description",
        "aliases",
        "merged_into",
        "embedding",
        "embedding_hash",
        "embedding_model",
    } <= set(first["kg_graph_node"]["columns"])
    assert {"source_id", "doc_key"} <= set(first["kg_doc_version"]["columns"])
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT graph_node_no, name FROM kg_graph_node WHERE id = 1")
        ).one() == ("Person:ada", "Ada Lovelace")


def test_migrate_identity_updates_endpoints_and_is_rerunnable(monkeypatch):
    engine = _legacy_engine()
    ensure_v3_schema(engine)
    monkeypatch.setattr(
        migrations_mod,
        "get_settings",
        lambda: SimpleNamespace(
            graph_no="g", graph_version=7, source_id="fixture-source"
        ),
    )

    with Session(engine) as session:
        ada = session.scalar(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada")
        )
        ada.properties = {"born": 1815}
        ada.ref = {"doc.md": ["1-2"]}
        ada.aliases = ["Ada_Lovelace", "Countess"]
        session.add_all(
            [
                GraphNode(
                    graph_no="g",
                    graph_version=7,
                    graph_node_no="Organization:analytical-engine",
                    name="Analytical Engine",
                    type="Organization",
                    ref={"doc.md": ["3-4"]},
                ),
                GraphEdge(
                    graph_no="g",
                    graph_version=7,
                    graph_edge_no="legacy-edge",
                    source_node_no="Person:ada",
                    target_node_no="Organization:analytical-engine",
                    name="works_for",
                    properties={"confidence": "EXTRACTED"},
                    ref={"doc.md": ["5-6"]},
                ),
            ]
        )
        session.execute(
            text(
                "INSERT INTO kg_doc_version "
                "(graph_no, graph_version, doc_id, content_hash) VALUES "
                "('g', 7, 'doc.md', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
            )
        )
        session.commit()

    first = migrate_identity(engine)

    assert first == MigrationStats(
        nodes_migrated=2,
        edges_updated=1,
        aliases_added=3,
        node_contributions_added=2,
        edge_contributions_added=1,
    )
    with Session(engine) as session:
        nodes = {
            node.name: node
            for node in session.scalars(
                select(GraphNode).where(
                    GraphNode.graph_no == "g", GraphNode.graph_version == 7
                )
            )
        }
        ada_no = nodes["Ada Lovelace"].graph_node_no
        engine_no = nodes["Analytical Engine"].graph_node_no
        for node_no in (ada_no, engine_no):
            assert node_no.startswith("node:")
            assert str(uuid.UUID(node_no[5:])) == node_no[5:]
        edge = session.scalar(select(GraphEdge))
        assert (edge.source_node_no, edge.target_node_no) == (ada_no, engine_no)
        assert len(edge.graph_edge_no) == 32
        int(edge.graph_edge_no, 16)
        assert session.query(LegacyNodeId).count() == 2
        assert session.query(NodeAlias).count() == 3
        assert session.query(NodeContribution).count() == 2
        assert session.query(EdgeContribution).count() == 1

    assert migrate_identity(engine) == MigrationStats()
    with Session(engine) as session:
        assert session.query(LegacyNodeId).count() == 2
        assert session.query(NodeAlias).count() == 3
        assert session.query(NodeContribution).count() == 2
        assert session.query(EdgeContribution).count() == 1


def test_migrate_multi_doc_legacy_payload_is_not_duplicated(monkeypatch):
    engine = _legacy_engine()
    ensure_v3_schema(engine)
    monkeypatch.setattr(
        migrations_mod,
        "get_settings",
        lambda: SimpleNamespace(
            graph_no="g", graph_version=7, source_id="fixture-source"
        ),
    )
    with Session(engine) as session:
        node = session.scalar(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada")
        )
        node.properties = {"fact_with_unknown_source": True}
        node.ref = {"a.md": ["1"], "b.md": ["2"]}
        session.commit()

    migrate_identity(engine)

    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(NodeContribution).order_by(NodeContribution.doc_id)
            )
        )
        assert [row.doc_id for row in rows] == ["a.md", "b.md"]
        assert all(row.properties == {} for row in rows)
        assert [row.ref for row in rows] == [
            {"fixture-source::a.md": ["1"]},
            {"fixture-source::b.md": ["2"]},
        ]

    assert migrate_identity(engine) == MigrationStats()
    with Session(engine) as session:
        assert session.query(LegacyNodeId).count() == 1
        assert session.query(NodeAlias).count() == 1
        assert session.query(NodeContribution).count() == 2
        assert session.query(EdgeContribution).count() == 0
