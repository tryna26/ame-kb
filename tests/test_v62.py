"""Offline V6.2 tests for durable task/run/step orchestration."""
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select

import ame_kb.pipeline as pipeline_mod
from ame_kb.manifest import ManifestFile
from ame_kb.models import Base, Graph, PipelineRun, PipelineStep, Task
from ame_kb.versioning import BuildResult, Classification
from ame_kb.taskqueue import RedisNotifier


def _sqlite_session_factory():
    from sqlalchemy import BigInteger, create_engine
    from sqlalchemy.dialects.mysql import LONGTEXT
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

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
    return sessionmaker(bind=engine, expire_on_commit=False)


def _scope_factory(Session):
    @contextmanager
    def _scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


class _Settings:
    pipeline_lease_seconds = 60
    pipeline_retry_delay_seconds = 0
    pipeline_poll_seconds = 0.01


def _setup(monkeypatch):
    Session = _sqlite_session_factory()
    monkeypatch.setattr(pipeline_mod, "session_scope", _scope_factory(Session))
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(pipeline_mod, "notify_task", lambda task_no: None)
    with Session() as session:
        session.add(Graph(graph_no="g1", graph_version=1, name="G1", status="ACTIVE"))
        session.commit()
    files = [
        ManifestFile("d1", "/tmp/d1.md", "md", ""),
        ManifestFile("d2", "/tmp/d2.md", "md", ""),
    ]
    return Session, files


def _plan():
    return BuildResult(
        graph_no="g1",
        base_version=1,
        target_version=2,
        classification=Classification(new=["d1", "d2"]),
    )


def test_enqueue_deduplicates_active_graph_task(monkeypatch):
    Session, files = _setup(monkeypatch)

    first = pipeline_mod.enqueue_ingest("g1", files=files)
    second = pipeline_mod.enqueue_ingest("g1", files=files)

    assert first.created is True
    assert second.created is False
    assert second.task_no == first.task_no
    with Session() as session:
        assert session.execute(select(Task)).scalars().all()[0].active_key == "g1"


def test_worker_success_persists_per_document_checkpoints(monkeypatch):
    Session, files = _setup(monkeypatch)
    task_no = pipeline_mod.enqueue_ingest("g1", files=files).task_no

    def _build(graph_no, manifest, *, dry_run=False, observer=None, **kwargs):
        plan = _plan()
        if not dry_run:
            for doc_no in plan.classification.to_extract:
                observer.doc_started(doc_no)
                observer.doc_succeeded(doc_no)
        return plan

    monkeypatch.setattr(pipeline_mod.versioning_mod, "build_next_version", _build)
    assert pipeline_mod.claim_task("worker-1") == task_no
    result = pipeline_mod.process_claimed_task(task_no)

    assert result.status == pipeline_mod.SUCCEEDED
    snapshot = pipeline_mod.task_snapshot(task_no)
    assert snapshot["status"] == pipeline_mod.SUCCEEDED
    assert (snapshot["progress_current"], snapshot["progress_total"]) == (2, 2)
    assert [s["status"] for s in snapshot["steps"]] == [
        pipeline_mod.SUCCEEDED,
        pipeline_mod.SUCCEEDED,
    ]
    with Session() as session:
        task = session.execute(select(Task)).scalar_one()
        assert task.active_key is None
        assert session.execute(select(PipelineRun)).scalar_one().status == "SUCCEEDED"


def test_failed_document_retries_and_resumes_checkpoint(monkeypatch):
    _, files = _setup(monkeypatch)
    task_no = pipeline_mod.enqueue_ingest(
        "g1", files=files, max_attempts=2
    ).task_no
    actual_calls = {"n": 0}

    def _build(graph_no, manifest, *, dry_run=False, observer=None, **kwargs):
        plan = _plan()
        if dry_run:
            return plan
        actual_calls["n"] += 1
        if actual_calls["n"] == 1:
            observer.doc_started("d1")
            error = RuntimeError("LLM timeout")
            observer.doc_failed("d1", error)
            raise error
        # The resumed version already contains d1, so versioning reports a
        # checkpoint skip and continues with d2.
        observer.doc_skipped("d1")
        observer.doc_started("d2")
        observer.doc_succeeded("d2")
        return plan

    monkeypatch.setattr(pipeline_mod.versioning_mod, "build_next_version", _build)

    assert pipeline_mod.claim_task("worker-1") == task_no
    first = pipeline_mod.process_claimed_task(task_no)
    assert first.status == pipeline_mod.QUEUED and first.will_retry

    assert pipeline_mod.claim_task("worker-2") == task_no
    second = pipeline_mod.process_claimed_task(task_no)
    assert second.status == pipeline_mod.SUCCEEDED
    snapshot = pipeline_mod.task_snapshot(task_no)
    assert snapshot["attempts"] == 2
    assert {s["doc_no"]: s["status"] for s in snapshot["steps"]} == {
        "d1": pipeline_mod.SKIPPED,
        "d2": pipeline_mod.SUCCEEDED,
    }


def test_terminal_failure_can_be_manually_requeued(monkeypatch):
    _, files = _setup(monkeypatch)
    task_no = pipeline_mod.enqueue_ingest(
        "g1", files=files, max_attempts=1
    ).task_no

    def _fail(graph_no, manifest, *, dry_run=False, observer=None, **kwargs):
        if dry_run:
            return _plan()
        observer.doc_started("d1")
        error = RuntimeError("bad document")
        observer.doc_failed("d1", error)
        raise error

    monkeypatch.setattr(pipeline_mod.versioning_mod, "build_next_version", _fail)
    pipeline_mod.claim_task("worker-1")
    result = pipeline_mod.process_claimed_task(task_no)
    assert result.status == pipeline_mod.FAILED

    pipeline_mod.retry_task(task_no, max_attempts=2)
    snapshot = pipeline_mod.task_snapshot(task_no)
    assert snapshot["status"] == pipeline_mod.QUEUED
    assert snapshot["attempts"] == 0
    assert snapshot["steps"][0]["status"] == pipeline_mod.PENDING


def test_expired_worker_lease_is_recovered(monkeypatch):
    Session, _ = _setup(monkeypatch)
    with Session() as session:
        task = Task(
            task_no="task_stale",
            task_type="INGEST",
            graph_no="g1",
            status=pipeline_mod.RUNNING,
            payload={},
            attempts=1,
            max_attempts=3,
            active_key="g1",
            available_at=datetime.utcnow(),
            lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
        )
        run = PipelineRun(
            run_no="run_stale",
            task_no="task_stale",
            graph_no="g1",
            target_version=2,
            status=pipeline_mod.RUNNING,
        )
        step = PipelineStep(
            step_no="step_stale",
            run_no="run_stale",
            step_key="d1",
            status=pipeline_mod.RUNNING,
        )
        session.add_all([task, run, step])
        session.commit()

    assert pipeline_mod.recover_expired_tasks() == ["task_stale"]
    snapshot = pipeline_mod.task_snapshot("task_stale")
    assert snapshot["status"] == pipeline_mod.QUEUED
    assert snapshot["run"]["status"] == pipeline_mod.RETRYING
    assert snapshot["steps"][0]["status"] == pipeline_mod.PENDING


def test_redis_notifier_only_transports_task_hints():
    class _Redis:
        def __init__(self):
            self.items = []

        def rpush(self, key, value):
            self.items.append((key, value))

        def blpop(self, key, timeout):
            if not self.items:
                return None
            item = self.items.pop(0)
            return item

    notifier = RedisNotifier("redis://unused/1", "ready")
    notifier._client = _Redis()
    notifier.notify("task_1")
    assert notifier.dequeue(1) == "task_1"
    assert notifier.dequeue(1) is None
