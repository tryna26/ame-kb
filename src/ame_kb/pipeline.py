"""V6.2 durable background ingest pipeline.

Execution flow:
  enqueue_ingest -> kg_task(QUEUED) -> worker claim with a lease -> dry-run plan
  -> kg_pipeline_run + one kg_pipeline_step per document -> versioning callbacks
  persist checkpoints after every document -> ACTIVE version -> task SUCCEEDED.

MySQL owns all state. Redis, when enabled, is only a best-effort wake-up queue;
workers always validate and claim tasks transactionally from MySQL.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterator, List, Optional

from sqlalchemy import func, or_, select

from . import manifest as manifest_mod
from . import versioning as versioning_mod
from .config import get_settings
from .db import session_scope
from .manifest import ManifestFile
from .models import Graph, PipelineRun, PipelineStep, Task
from .taskqueue import dequeue_hint, notify_task

QUEUED = "QUEUED"
RUNNING = "RUNNING"
RETRYING = "RETRYING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
PENDING = "PENDING"
SKIPPED = "SKIPPED"
CANCELLED = "CANCELLED"

ACTIVE_TASK_STATUSES = (QUEUED, RUNNING)
COMPLETED_STEP_STATUSES = (SUCCEEDED, SKIPPED)


@dataclass
class EnqueueResult:
    task_no: str
    created: bool
    warning: Optional[str] = None


@dataclass
class ProcessResult:
    task_no: str
    status: str
    will_retry: bool = False
    error: Optional[str] = None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.utcnow()


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:8000]


def enqueue_ingest(
    graph_no: str,
    *,
    force: bool = False,
    allow_empty: bool = False,
    max_attempts: int = 3,
    files: Optional[List[ManifestFile]] = None,
) -> EnqueueResult:
    """Create one durable ingest task per graph, deduplicating active work."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    files = list(files if files is not None else manifest_mod.list_files(graph_no))
    if not files:
        raise ValueError(f"manifest for {graph_no} is empty")

    task_no = ""
    created = False
    with session_scope() as session:
        # Serialize enqueue for a graph. The nullable unique active_key is a
        # second line of defence against duplicate active tasks.
        graph_row = session.execute(
            select(Graph)
            .where(Graph.graph_no == graph_no)
            .order_by(Graph.graph_version.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if graph_row is None:
            raise ValueError(f"unknown managed graph: {graph_no}")

        existing = session.execute(
            select(Task).where(
                Task.active_key == graph_no,
                Task.status.in_(ACTIVE_TASK_STATUSES),
            )
        ).scalar_one_or_none()
        if existing is not None:
            task_no = existing.task_no
        else:
            task_no = _id("task")
            session.add(
                Task(
                    task_no=task_no,
                    task_type="INGEST",
                    graph_no=graph_no,
                    status=QUEUED,
                    payload={
                        "force": force,
                        "allow_empty": allow_empty,
                        "files": [asdict(f) for f in files],
                    },
                    max_attempts=max_attempts,
                    available_at=_now(),
                    active_key=graph_no,
                )
            )
            created = True

    warning = notify_task(task_no)
    return EnqueueResult(task_no, created, warning)


def claim_task(worker_id: str, task_no: Optional[str] = None) -> Optional[str]:
    """Atomically lease one eligible task, optionally validating a Redis hint."""

    settings = get_settings()
    now = _now()
    with session_scope() as session:
        stmt = select(Task).where(
            Task.status == QUEUED,
            Task.available_at <= now,
        )
        if task_no is not None:
            stmt = stmt.where(Task.task_no == task_no)
        else:
            stmt = stmt.order_by(Task.available_at, Task.create_time)
        task = session.execute(
            stmt.limit(1).with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if task is None:
            return None
        task.status = RUNNING
        task.attempts += 1
        task.worker_id = worker_id
        task.lease_expires_at = now + timedelta(
            seconds=settings.pipeline_lease_seconds
        )
        return task.task_no


def _files_from_payload(payload: dict) -> List[ManifestFile]:
    return [ManifestFile(**row) for row in (payload.get("files") or [])]


def _renew_lease(task_no: str) -> None:
    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no)
        ).scalar_one_or_none()
        if task is not None and task.status == RUNNING:
            task.lease_expires_at = _now() + timedelta(
                seconds=get_settings().pipeline_lease_seconds
            )


@contextmanager
def _lease_heartbeat(task_no: str) -> Iterator[None]:
    """Keep long projection/LLM calls leased between document checkpoints."""

    stop = threading.Event()
    interval = max(1.0, get_settings().pipeline_lease_seconds / 3)

    def _beat() -> None:
        while not stop.wait(interval):
            try:
                _renew_lease(task_no)
            except Exception:
                # The main execution path remains authoritative. A transient
                # heartbeat failure is retried on the next interval.
                continue

    thread = threading.Thread(
        target=_beat, name=f"lease-{task_no[:16]}", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def _ensure_run(task_no: str, plan: versioning_mod.BuildResult) -> str:
    """Create/update the run and its per-document checkpoint rows."""

    now = _now()
    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no)
        ).scalar_one()
        run = session.execute(
            select(PipelineRun).where(PipelineRun.task_no == task_no)
        ).scalar_one_or_none()
        if run is None:
            run = PipelineRun(
                run_no=_id("run"),
                task_no=task_no,
                graph_no=task.graph_no,
                base_version=plan.base_version,
                target_version=plan.target_version,
                status=RUNNING,
                started_at=now,
            )
            session.add(run)
            session.flush()
        else:
            run.base_version = plan.base_version
            run.target_version = plan.target_version
            run.status = RUNNING
            run.error = None
            run.finished_at = None
            run.started_at = run.started_at or now

        existing = {
            row.step_key: row
            for row in session.execute(
                select(PipelineStep).where(PipelineStep.run_no == run.run_no)
            ).scalars()
        }
        planned_keys = set(plan.classification.to_extract)
        for doc_no in planned_keys:
            if doc_no not in existing:
                session.add(
                    PipelineStep(
                        step_no=_id("step"),
                        run_no=run.run_no,
                        step_key=doc_no,
                        status=PENDING,
                        max_attempts=task.max_attempts,
                    )
                )
        # A retry may observe a file that reverted to the base hash or vanished
        # from the snapshotted path. Such an old failed/pending step is no longer
        # required by the current plan and becomes a completed skip.
        for doc_no, step in existing.items():
            if doc_no not in planned_keys and step.status not in COMPLETED_STEP_STATUSES:
                step.status = SKIPPED
                step.error = None
                step.finished_at = now
        session.flush()
        total = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == run.run_no
            )
        ).scalar_one()
        completed = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == run.run_no,
                PipelineStep.status.in_(COMPLETED_STEP_STATUSES),
            )
        ).scalar_one()
        failed = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == run.run_no,
                PipelineStep.status == FAILED,
            )
        ).scalar_one()
        run.total_steps = total
        run.completed_steps = completed
        run.failed_steps = failed
        task.progress_total = total
        task.progress_current = completed
        return run.run_no


class PipelineObserver:
    """Versioning observer that commits a durable checkpoint per document."""

    def __init__(self, task_no: str, run_no: str):
        self.task_no = task_no
        self.run_no = run_no

    def _ensure_step(self, session, doc_no: str) -> PipelineStep:
        step = session.execute(
            select(PipelineStep).where(
                PipelineStep.run_no == self.run_no,
                PipelineStep.step_key == doc_no,
            )
        ).scalar_one_or_none()
        if step is None:
            task = session.execute(
                select(Task).where(Task.task_no == self.task_no)
            ).scalar_one()
            step = PipelineStep(
                step_no=_id("step"),
                run_no=self.run_no,
                step_key=doc_no,
                status=PENDING,
                max_attempts=task.max_attempts,
            )
            session.add(step)
            session.flush()
        return step

    def _refresh(self, session) -> None:
        run = session.execute(
            select(PipelineRun).where(PipelineRun.run_no == self.run_no)
        ).scalar_one()
        task = session.execute(
            select(Task).where(Task.task_no == self.task_no)
        ).scalar_one()
        total = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == self.run_no
            )
        ).scalar_one()
        completed = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == self.run_no,
                PipelineStep.status.in_(COMPLETED_STEP_STATUSES),
            )
        ).scalar_one()
        failed = session.execute(
            select(func.count(PipelineStep.id)).where(
                PipelineStep.run_no == self.run_no,
                PipelineStep.status == FAILED,
            )
        ).scalar_one()
        run.total_steps = total
        run.completed_steps = completed
        run.failed_steps = failed
        task.progress_total = total
        task.progress_current = completed
        task.lease_expires_at = _now() + timedelta(
            seconds=get_settings().pipeline_lease_seconds
        )

    def doc_started(self, doc_no: str) -> None:
        with session_scope() as session:
            step = self._ensure_step(session, doc_no)
            step.status = RUNNING
            step.attempts += 1
            step.error = None
            step.started_at = _now()
            step.finished_at = None
            self._refresh(session)

    def doc_succeeded(self, doc_no: str) -> None:
        with session_scope() as session:
            step = self._ensure_step(session, doc_no)
            step.status = SUCCEEDED
            step.error = None
            step.finished_at = _now()
            self._refresh(session)

    def doc_skipped(self, doc_no: str) -> None:
        with session_scope() as session:
            step = self._ensure_step(session, doc_no)
            # A previous attempt may already have committed this document. Keep
            # SUCCEEDED as the stronger checkpoint; otherwise record SKIPPED.
            if step.status != SUCCEEDED:
                step.status = SKIPPED
            step.error = None
            step.finished_at = step.finished_at or _now()
            self._refresh(session)

    def doc_failed(self, doc_no: str, error: Exception) -> None:
        with session_scope() as session:
            step = self._ensure_step(session, doc_no)
            step.status = FAILED
            step.error = _error_text(error)
            step.finished_at = _now()
            self._refresh(session)


def _mark_succeeded(task_no: str, run_no: str) -> None:
    now = _now()
    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no)
        ).scalar_one()
        run = session.execute(
            select(PipelineRun).where(PipelineRun.run_no == run_no)
        ).scalar_one()
        task.status = SUCCEEDED
        task.progress_current = task.progress_total
        task.active_key = None
        task.lease_expires_at = None
        task.worker_id = ""
        task.error = None
        run.status = SUCCEEDED
        run.completed_steps = run.total_steps
        run.failed_steps = 0
        run.error = None
        run.finished_at = now


def _mark_failed(task_no: str, error: Exception) -> bool:
    """Persist failure and return True when an automatic retry was scheduled."""

    settings = get_settings()
    message = _error_text(error)
    retry = False
    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no).with_for_update()
        ).scalar_one()
        run = session.execute(
            select(PipelineRun).where(PipelineRun.task_no == task_no)
        ).scalar_one_or_none()
        task.error = message
        task.lease_expires_at = None
        task.worker_id = ""
        if task.attempts < task.max_attempts:
            retry = True
            task.status = QUEUED
            task.available_at = _now() + timedelta(
                seconds=settings.pipeline_retry_delay_seconds
            )
            if run is not None:
                run.status = RETRYING
                run.error = message
        else:
            task.status = FAILED
            task.active_key = None
            if run is not None:
                run.status = FAILED
                run.error = message
                run.finished_at = _now()
    if retry:
        notify_task(task_no)
    return retry


def process_claimed_task(task_no: str) -> ProcessResult:
    """Execute a RUNNING task; failures become durable retry/terminal state."""

    run_no: Optional[str] = None
    try:
        with session_scope() as session:
            task = session.execute(
                select(Task).where(Task.task_no == task_no)
            ).scalar_one()
            if task.status != RUNNING:
                raise ValueError(f"task {task_no} is not RUNNING")
            if task.task_type != "INGEST":
                raise ValueError(f"unsupported task type: {task.task_type}")
            graph_no = task.graph_no
            payload: Dict = dict(task.payload or {})

        with _lease_heartbeat(task_no):
            files = _files_from_payload(payload)
            if not files:
                raise ValueError("task manifest snapshot is empty")
            plan = versioning_mod.build_next_version(
                graph_no,
                files,
                force=bool(payload.get("force")),
                allow_empty=bool(payload.get("allow_empty")),
                dry_run=True,
            )
            run_no = _ensure_run(task_no, plan)
            observer = PipelineObserver(task_no, run_no)
            versioning_mod.build_next_version(
                graph_no,
                files,
                force=bool(payload.get("force")),
                allow_empty=bool(payload.get("allow_empty")),
                observer=observer,
            )
        _mark_succeeded(task_no, run_no)
        return ProcessResult(task_no, SUCCEEDED)
    except Exception as exc:  # noqa: BLE001 - convert into durable task state
        retry = _mark_failed(task_no, exc)
        return ProcessResult(
            task_no,
            QUEUED if retry else FAILED,
            will_retry=retry,
            error=_error_text(exc),
        )


def recover_expired_tasks() -> List[str]:
    """Requeue tasks whose worker lease expired; fail exhausted tasks."""

    now = _now()
    requeued: List[str] = []
    with session_scope() as session:
        tasks = (
            session.execute(
                select(Task)
                .where(
                    Task.status == RUNNING,
                    or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now),
                )
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for task in tasks:
            run = session.execute(
                select(PipelineRun).where(PipelineRun.task_no == task.task_no)
            ).scalar_one_or_none()
            exhausted = task.attempts >= task.max_attempts
            task.status = FAILED if exhausted else QUEUED
            task.active_key = None if exhausted else task.graph_no
            task.available_at = now
            task.lease_expires_at = None
            task.worker_id = ""
            task.error = "worker lease expired"
            if run is not None:
                run.status = FAILED if exhausted else RETRYING
                run.error = task.error
                if exhausted:
                    run.finished_at = now
                for step in session.execute(
                    select(PipelineStep).where(
                        PipelineStep.run_no == run.run_no,
                        PipelineStep.status == RUNNING,
                    )
                ).scalars():
                    step.status = FAILED if exhausted else PENDING
                    step.error = task.error
                    if exhausted:
                        step.finished_at = now
            if not exhausted:
                requeued.append(task.task_no)
    for task_no in requeued:
        notify_task(task_no)
    return requeued


def retry_task(task_no: str, max_attempts: Optional[int] = None) -> None:
    """Manually requeue a terminal FAILED task while keeping completed steps."""

    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no).with_for_update()
        ).scalar_one_or_none()
        if task is None:
            raise ValueError(f"unknown task: {task_no}")
        if task.status != FAILED:
            raise ValueError(f"task {task_no} is {task.status}, not FAILED")
        active = session.execute(
            select(Task.id).where(
                Task.active_key == task.graph_no,
                Task.id != task.id,
            )
        ).scalar_one_or_none()
        if active is not None:
            raise ValueError(f"graph {task.graph_no} already has an active task")
        task.status = QUEUED
        task.attempts = 0
        if max_attempts is not None:
            if max_attempts < 1:
                raise ValueError("max_attempts must be >= 1")
            task.max_attempts = max_attempts
        task.available_at = _now()
        task.active_key = task.graph_no
        task.error = None
        run = session.execute(
            select(PipelineRun).where(PipelineRun.task_no == task_no)
        ).scalar_one_or_none()
        if run is not None:
            run.status = RETRYING
            run.error = None
            run.finished_at = None
            for step in session.execute(
                select(PipelineStep).where(
                    PipelineStep.run_no == run.run_no,
                    PipelineStep.status == FAILED,
                )
            ).scalars():
                step.status = PENDING
                step.error = None
                step.finished_at = None
                step.max_attempts = task.max_attempts
    notify_task(task_no)


def task_snapshot(task_no: str) -> dict:
    """Return task/run/step progress as a JSON-serializable dictionary."""

    with session_scope() as session:
        task = session.execute(
            select(Task).where(Task.task_no == task_no)
        ).scalar_one_or_none()
        if task is None:
            raise ValueError(f"unknown task: {task_no}")
        run = session.execute(
            select(PipelineRun).where(PipelineRun.task_no == task_no)
        ).scalar_one_or_none()
        steps = []
        if run is not None:
            steps = (
                session.execute(
                    select(PipelineStep)
                    .where(PipelineStep.run_no == run.run_no)
                    .order_by(PipelineStep.step_key)
                )
                .scalars()
                .all()
            )
        return {
            "task_no": task.task_no,
            "task_type": task.task_type,
            "graph_no": task.graph_no,
            "status": task.status,
            "progress_current": task.progress_current,
            "progress_total": task.progress_total,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            "worker_id": task.worker_id,
            "error": task.error,
            "run": None
            if run is None
            else {
                "run_no": run.run_no,
                "status": run.status,
                "base_version": run.base_version,
                "target_version": run.target_version,
                "completed_steps": run.completed_steps,
                "total_steps": run.total_steps,
                "failed_steps": run.failed_steps,
                "error": run.error,
            },
            "steps": [
                {
                    "step_no": step.step_no,
                    "doc_no": step.step_key,
                    "status": step.status,
                    "attempts": step.attempts,
                    "max_attempts": step.max_attempts,
                    "error": step.error,
                }
                for step in steps
            ],
        }


def list_tasks(graph_no: Optional[str] = None, limit: int = 20) -> List[dict]:
    if limit < 1:
        return []
    with session_scope() as session:
        stmt = select(Task)
        if graph_no:
            stmt = stmt.where(Task.graph_no == graph_no)
        rows = (
            session.execute(stmt.order_by(Task.create_time.desc()).limit(limit))
            .scalars()
            .all()
        )
        return [
            {
                "task_no": row.task_no,
                "graph_no": row.graph_no,
                "status": row.status,
                "progress_current": row.progress_current,
                "progress_total": row.progress_total,
                "attempts": row.attempts,
                "max_attempts": row.max_attempts,
                "error": row.error,
            }
            for row in rows
        ]


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def run_worker(
    *,
    once: bool = False,
    max_tasks: int = 0,
    worker_id: Optional[str] = None,
    on_result: Optional[Callable[[ProcessResult], None]] = None,
) -> List[ProcessResult]:
    """Poll/consume tasks until interrupted, or bounded by once/max_tasks."""

    settings = get_settings()
    worker_id = worker_id or default_worker_id()
    recover_expired_tasks()
    results: List[ProcessResult] = []
    while max_tasks <= 0 or len(results) < max_tasks:
        timeout = settings.pipeline_poll_seconds
        hint = None if once else dequeue_hint(timeout)
        claimed = claim_task(worker_id, hint) if hint else None
        claimed = claimed or claim_task(worker_id)
        if claimed is None:
            if once:
                break
            time.sleep(max(0.1, timeout))
            continue
        result = process_claimed_task(claimed)
        results.append(result)
        if on_result is not None:
            on_result(result)
        if once:
            break
    return results
