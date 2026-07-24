"""Offline V5 tests: fusion helpers, alias indexing, cluster parsing,
merge/rollback, and low-support pruning against an in-memory fake session.
No real DB/LLM/embedding calls."""
import ame_kb.resolve as resolve_mod
from ame_kb.searchindex import build_searchable_text
from ame_kb.store import merge_props, merge_ref_maps


# ---- field-union merge helpers (store.py) ----

def test_merge_props_winner_wins_loser_fills():
    winner = {"title": "Chief Architect", "office": "Seattle"}
    loser = {"title": "Architect", "born": "1990"}
    out = merge_props(winner, loser)
    assert out["title"] == "Chief Architect"  # winner wins conflict
    assert out["office"] == "Seattle"
    assert out["born"] == "1990"  # loser fills missing key


def test_merge_props_handles_none():
    assert merge_props(None, {"a": 1}) == {"a": 1}
    assert merge_props({"a": 1}, None) == {"a": 1}


def test_merge_ref_maps_unions_per_doc():
    a = {"d1": ["1", "2"], "d2": ["5"]}
    b = {"d1": ["2", "3"], "d3": ["9"]}
    out = merge_ref_maps(a, b)
    assert out["d1"] == ["1", "2", "3"]  # union + sorted, dedup
    assert out["d2"] == ["5"]
    assert out["d3"] == ["9"]


# ---- aliases fold into searchable_text (searchindex.py) ----

def test_searchable_text_includes_aliases():
    text = build_searchable_text(
        "Ada Lovelace", "数学家", {}, aliases=["Ada", "阿达"]
    )
    assert "Ada Lovelace" in text
    assert "Ada" in text and "阿达" in text


def test_searchable_text_no_aliases_backcompat():
    # Old 3-arg calls still work (aliases optional).
    assert build_searchable_text("N", None, {}) == "N"


# ---- cluster parsing (resolve.py) ----

def test_cluster_parses_group(monkeypatch):
    monkeypatch.setattr(
        resolve_mod,
        "call_llm",
        lambda p: '{"clusters":[{"canonical":"Ada Lovelace","names":["Ada","Ada Lovelace"]}]}',
    )
    out = resolve_mod.cluster(
        "Person", [{"name": "Ada"}, {"name": "Ada Lovelace"}]
    )
    assert len(out) == 1
    assert out[0]["canonical"] == "Ada Lovelace"
    assert set(out[0]["names"]) == {"Ada", "Ada Lovelace"}


def test_cluster_drops_invented_names(monkeypatch):
    # The model must only reuse names it was given; invented names are dropped,
    # so a group that shrinks below 2 real names is discarded.
    monkeypatch.setattr(
        resolve_mod,
        "call_llm",
        lambda p: '{"clusters":[{"canonical":"X","names":["Ada","Ghost"]}]}',
    )
    out = resolve_mod.cluster(
        "Person", [{"name": "Ada"}, {"name": "Bob"}]
    )
    assert out == []


def test_cluster_bad_json_is_empty(monkeypatch):
    monkeypatch.setattr(resolve_mod, "call_llm", lambda p: "totally not json")
    assert resolve_mod.cluster("Person", [{"name": "A"}, {"name": "B"}]) == []


def test_cluster_llm_failure_is_empty(monkeypatch):
    def _boom(p):
        raise RuntimeError("no llm")

    monkeypatch.setattr(resolve_mod, "call_llm", _boom)
    assert resolve_mod.cluster("Person", [{"name": "A"}, {"name": "B"}]) == []


def test_cluster_single_entity_short_circuits(monkeypatch):
    # Fewer than 2 candidates: never calls the LLM.
    def _boom(p):
        raise AssertionError("should not call LLM")

    monkeypatch.setattr(resolve_mod, "call_llm", _boom)
    assert resolve_mod.cluster("Person", [{"name": "Solo"}]) == []


# ---- merge + rollback against a real in-memory SQLite DB ----

def test_merge_and_rollback_end_to_end(monkeypatch):
    from sqlalchemy import select

    from ame_kb.models import EntityAlias, GraphEdge, GraphNode, MergeLog
    from ame_kb.store import edge_no

    Session = _sqlite_session_factory()
    settings = _FakeSettings()

    # Seed: two duplicate Person nodes + an edge on the loser + its target.
    with Session() as s:
        s.add_all(
            [
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:ada", name="Ada", type="Person",
                          description="", properties={"office": "London"}, ref={"d1": ["1"]}),
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:ada-lovelace", name="Ada Lovelace",
                          type="Person", description="数学家",
                          properties={"born": "1815"}, ref={"d2": ["3"]}),
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Document:notes", name="Notes", type="Document",
                          properties={}, ref={}),
                GraphEdge(graph_no="default", graph_version=1,
                          graph_edge_no=edge_no("Person:ada-lovelace", "authored", "Document:notes"),
                          source_node_no="Person:ada-lovelace", target_node_no="Document:notes",
                          name="authored", properties={}, ref={}),
            ]
        )
        s.commit()

    _wire_resolve_sqlite(monkeypatch, Session, settings)

    merge_id = resolve_mod.merge(
        "Person:ada", "Person:ada-lovelace", canonical_name="Ada Lovelace"
    )

    with Session() as s:
        winner = s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada")
        ).scalar_one()
        loser = s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada-lovelace")
        ).scalar_one()
        assert loser.deleted == 1
        assert winner.name == "Ada Lovelace"  # adopted more complete name
        assert winner.properties["office"] == "London"  # winner kept
        assert winner.properties["born"] == "1815"  # loser field folded in
        assert winner.ref == {"d1": ["1"], "d2": ["3"]}  # ref unioned per doc
        aliases = {
            a.alias
            for a in s.execute(
                select(EntityAlias).where(
                    EntityAlias.canonical_node_no == "Person:ada"
                )
            ).scalars()
        }
        assert aliases == {"Ada", "Ada Lovelace"}  # loser name + old winner name
        # edge repointed to the winner.
        edge = s.execute(select(GraphEdge)).scalars().one()
        assert edge.source_node_no == "Person:ada"
        assert edge.deleted == 0
        assert s.execute(
            select(MergeLog).where(MergeLog.merge_id == merge_id)
        ).scalar_one().status == "MERGED"

    # rollback restores everything.
    resolve_mod.rollback(merge_id)

    with Session() as s:
        assert s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada-lovelace")
        ).scalar_one().deleted == 0
        winner = s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:ada")
        ).scalar_one()
        assert winner.name == "Ada"  # name restored
        assert "born" not in winner.properties  # props restored
        assert winner.ref == {"d1": ["1"]}  # ref restored
        edge = s.execute(select(GraphEdge)).scalars().one()
        assert edge.source_node_no == "Person:ada-lovelace"  # edge un-remapped
        assert (
            s.execute(
                select(EntityAlias).where(
                    EntityAlias.canonical_node_no == "Person:ada"
                )
            ).first()
            is None
        )  # aliases dropped
        assert s.execute(
            select(MergeLog).where(MergeLog.merge_id == merge_id)
        ).scalar_one().status == "ROLLED_BACK"


def test_merge_dedups_colliding_edge(monkeypatch):
    # Winner already has authored->notes ; loser also authored->notes. After
    # remap the two collapse into one (dedup), and rollback splits them again.
    from sqlalchemy import select

    from ame_kb.models import GraphEdge, GraphNode
    from ame_kb.store import edge_no

    Session = _sqlite_session_factory()
    settings = _FakeSettings()
    with Session() as s:
        s.add_all(
            [
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:a", name="A", type="Person",
                          properties={}, ref={}),
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:b", name="B", type="Person",
                          properties={}, ref={}),
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Document:n", name="N", type="Document",
                          properties={}, ref={}),
                GraphEdge(graph_no="default", graph_version=1,
                          graph_edge_no=edge_no("Person:a", "authored", "Document:n"),
                          source_node_no="Person:a", target_node_no="Document:n",
                          name="authored", properties={"confidence": "INFERRED"}, ref={}),
                GraphEdge(graph_no="default", graph_version=1,
                          graph_edge_no=edge_no("Person:b", "authored", "Document:n"),
                          source_node_no="Person:b", target_node_no="Document:n",
                          name="authored", properties={"confidence": "EXTRACTED"}, ref={"d": ["9"]}),
            ]
        )
        s.commit()

    _wire_resolve_sqlite(monkeypatch, Session, settings)
    resolve_mod.merge("Person:a", "Person:b")

    with Session() as s:
        live = s.execute(
            select(GraphEdge).where(GraphEdge.deleted == 0)
        ).scalars().all()
        assert len(live) == 1  # collapsed to one
        assert live[0].source_node_no == "Person:a"
        assert live[0].ref == {"d": ["9"]}  # loser edge's ref folded in


# ---- low-support pruning (resolve.py) ----

def test_prune_low_support_drops_orphans_and_rolls_back(monkeypatch):
    from sqlalchemy import select

    from ame_kb.models import GraphEdge, GraphNode, MergeLog

    Session = _sqlite_session_factory()
    settings = _FakeSettings()
    with Session() as s:
        s.add_all(
            [
                # support=1, no edge -> pruned
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:orphan", name="Orphan",
                          type="ENTITY", properties={}, ref={"d1": ["1"]}),
                # support=2 -> kept (enough support)
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:popular", name="Popular",
                          type="ENTITY", properties={}, ref={"d1": ["1"], "d2": ["3"]}),
                # support=1 but edge-referenced -> protected
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Person:linked", name="Linked",
                          type="ENTITY", properties={}, ref={"d1": ["1"]}),
                GraphNode(graph_no="default", graph_version=1,
                          graph_node_no="Doc:n", name="N", type="ENTITY",
                          properties={}, ref={"d1": ["1"], "d2": ["2"]}),
                GraphEdge(graph_no="default", graph_version=1,
                          graph_edge_no="e1", source_node_no="Person:linked",
                          target_node_no="Doc:n", name="in", properties={}, ref={}),
            ]
        )
        s.commit()

    _wire_resolve_sqlite(monkeypatch, Session, settings)
    stats = resolve_mod.prune_low_support()
    assert stats.pruned == 1

    with Session() as s:
        orphan = s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:orphan")
        ).scalar_one()
        assert orphan.deleted == 1
        assert s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:linked")
        ).scalar_one().deleted == 0  # protected by edge
        assert s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:popular")
        ).scalar_one().deleted == 0  # enough support
        log = s.execute(
            select(MergeLog).where(MergeLog.status == "PRUNED")
        ).scalar_one()
        merge_id = log.merge_id
        assert log.loser_node_no == "Person:orphan"

    # rollback restores the pruned node.
    resolve_mod.rollback(merge_id)
    with Session() as s:
        assert s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:orphan")
        ).scalar_one().deleted == 0
        assert s.execute(
            select(MergeLog).where(MergeLog.merge_id == merge_id)
        ).scalar_one().status == "ROLLED_BACK"


def test_prune_dry_run_writes_nothing(monkeypatch):
    from sqlalchemy import select

    from ame_kb.models import GraphNode, MergeLog

    Session = _sqlite_session_factory()
    settings = _FakeSettings()
    with Session() as s:
        s.add(
            GraphNode(graph_no="default", graph_version=1,
                      graph_node_no="Person:orphan", name="Orphan",
                      type="ENTITY", properties={}, ref={"d1": ["1"]})
        )
        s.commit()

    _wire_resolve_sqlite(monkeypatch, Session, settings)
    stats = resolve_mod.prune_low_support(dry_run=True)
    assert stats.pruned == 1
    with Session() as s:
        assert s.execute(
            select(GraphNode).where(GraphNode.graph_node_no == "Person:orphan")
        ).scalar_one().deleted == 0  # untouched
        assert s.execute(select(MergeLog)).first() is None


# ---- fakes / fixtures ----

class _FakeSettings:
    def __init__(self):
        self.graph_no = "default"
        self.graph_version = 1
        self.resolve_candidate_topk = 10
        self.resolve_min_score_embedding = 0.0
        self.min_score_text = 0.0
        self.resolve_prune_enabled = False
        self.resolve_prune_min_support = 2


def _sqlite_session_factory():
    """A shared in-memory SQLite bound to the MySQL-flavoured ORM models."""
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


class _NoopIndex:
    def delete_objects(self, *a, **k):
        return 0

    def upsert(self, *a, **k):
        return 0


def _wire_resolve_sqlite(monkeypatch, Session, settings):
    """Point resolve.py at the SQLite DB; stub the search index (no embeddings
    offline). Reindex helpers become no-ops so we test only the graph mutations."""
    from contextlib import contextmanager

    monkeypatch.setattr(resolve_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(resolve_mod, "get_index", lambda: _NoopIndex())
    monkeypatch.setattr(resolve_mod, "_reindex_node", lambda *a, **k: None)
    monkeypatch.setattr(resolve_mod, "_reindex_edge", lambda *a, **k: None)

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

    monkeypatch.setattr(resolve_mod, "session_scope", _scope)

