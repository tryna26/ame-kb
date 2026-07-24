"""V6.3 service facade shared by the CLI and the REST API.

A thin layer over the existing modules whose single job is to run every
operation inside a resolved, request/task-local graph context. Selecting a graph
per call (rather than mutating process-wide state) keeps concurrent REST/MCP
requests isolated while leaving the underlying module functions untouched.

Nothing here changes behaviour: each function resolves the target graph +
version, enters ``config.graph_context``, and delegates to the module that
already implements the logic (recall, pipeline, graphs, query, manifest).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, List, Optional

from . import graphs as graphs_mod
from . import manifest as manifest_mod
from . import pipeline as pipeline_mod
from .config import get_settings, graph_context
from .graphs import GraphInfo
from .manifest import ManifestFile
from .pipeline import EnqueueResult, ProcessResult
from .query import NodeHit, RelationHit, find_entities as _find_entities
from .query import relations_of as _relations_of
from .query import GraphSnapshot, graph_snapshot as _graph_snapshot
from .recall import RecallResult, recall as _recall
from .resolve import ResolveStats, alias_of as _alias_of
from .resolve import resolve_all as _resolve_all, rollback as _rollback

DEFAULT_GRAPH_NO = graphs_mod.DEFAULT_GRAPH_NO


def _resolve_graph(
    graph_no: Optional[str], graph_version: Optional[int]
) -> tuple[str, int]:
    """Resolve (graph_no, version) for a call.

    When ``graph_no`` is omitted (None), inherit the caller's already-resolved
    graph context: the CLI callback runs ``apply_graph_context`` before the
    command, so ``--graph-no`` / ``--graph-version`` are honoured. Only when a
    graph_no is passed explicitly (e.g. a REST request naming a graph) do we
    resolve afresh: graph_version None -> the graph's latest ACTIVE version (the
    default-graph sentinel keeps its configured version and never triggers a
    kg_graph lookup, mirroring graphs.apply_graph_context).
    """
    current = get_settings()
    if graph_no is None:
        return current.graph_no, (
            graph_version if graph_version is not None else current.graph_version
        )
    active_no = graph_no
    active_version = graph_version
    if active_version is None and active_no != DEFAULT_GRAPH_NO:
        active_version = graphs_mod._latest_version_safe(active_no)
    if active_version is None:
        active_version = current.graph_version
    return active_no, active_version


@contextmanager
def _graph_scope(
    graph_no: Optional[str], graph_version: Optional[int]
) -> Iterator[tuple[str, int]]:
    active_no, active_version = _resolve_graph(graph_no, graph_version)
    with graph_context(active_no, active_version):
        yield active_no, active_version


# --------------------------------------------------------------------------
# Recall / query
# --------------------------------------------------------------------------


def search(
    query: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
    window: int = 0,
    trace: bool = False,
    state_id: Optional[str] = None,
) -> RecallResult:
    with _graph_scope(graph_no, graph_version):
        return _recall(query, window=window, trace=trace, state_id=state_id)


def find_entities(
    name: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
    limit: int = 20,
) -> List[NodeHit]:
    with _graph_scope(graph_no, graph_version):
        return _find_entities(name, limit=limit)


def relations_of(
    node_no: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
) -> List[RelationHit]:
    with _graph_scope(graph_no, graph_version):
        return _relations_of(node_no)


def graph_snapshot(
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
    limit: int = 2000,
) -> GraphSnapshot:
    with _graph_scope(graph_no, graph_version):
        return _graph_snapshot(limit=limit)


def clear_recall_state(
    state_id: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
) -> bool:
    """Forget an explored-state so the next search under it starts fresh.

    The store key is namespaced by graph (same as ``recall``), so clearing must
    resolve the same graph scope the search ran under.
    """
    from .recall_state import clear_state, state_key

    active_no, active_version = _resolve_graph(graph_no, graph_version)
    return clear_state(state_key(active_no, active_version, state_id))


# --------------------------------------------------------------------------
# Entity fusion (resolve)
# --------------------------------------------------------------------------


def resolve_all(
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
    type_filter: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    prune: Optional[bool] = None,
) -> ResolveStats:
    with _graph_scope(graph_no, graph_version):
        return _resolve_all(
            type_filter=type_filter, dry_run=dry_run, limit=limit, prune=prune
        )


def rollback_merge(
    merge_id: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
) -> None:
    with _graph_scope(graph_no, graph_version):
        _rollback(merge_id)


def alias_of(
    entity: str,
    *,
    graph_no: Optional[str] = None,
    graph_version: Optional[int] = None,
) -> List[str]:
    with _graph_scope(graph_no, graph_version):
        return _alias_of(entity)


# --------------------------------------------------------------------------
# Durable pipeline
# --------------------------------------------------------------------------


def enqueue_ingest(
    graph_no: str,
    *,
    force: bool = False,
    allow_empty: bool = False,
    max_attempts: int = 3,
) -> EnqueueResult:
    with _graph_scope(graph_no, None):
        if not graphs_mod.is_managed(graph_no):
            raise ValueError(f"not a managed graph: {graph_no}")
        return pipeline_mod.enqueue_ingest(
            graph_no,
            force=force,
            allow_empty=allow_empty,
            max_attempts=max_attempts,
        )


def task_status(task_no: str) -> dict:
    return pipeline_mod.task_snapshot(task_no)


def list_tasks(graph_no: Optional[str] = None, limit: int = 20) -> List[dict]:
    return pipeline_mod.list_tasks(graph_no, limit)


def retry_task(task_no: str, *, max_attempts: Optional[int] = None) -> None:
    pipeline_mod.retry_task(task_no, max_attempts=max_attempts)


def run_worker(
    *,
    once: bool = False,
    max_tasks: int = 0,
    worker_id: Optional[str] = None,
    on_result=None,
) -> List[ProcessResult]:
    return pipeline_mod.run_worker(
        once=once, max_tasks=max_tasks, worker_id=worker_id, on_result=on_result
    )


# --------------------------------------------------------------------------
# Graph registry + manifest
# --------------------------------------------------------------------------


def list_graphs() -> List[GraphInfo]:
    return graphs_mod.list_graphs()


def create_graph(name: str) -> str:
    return graphs_mod.create_graph(name)


def list_files(graph_no: str) -> List[ManifestFile]:
    with _graph_scope(graph_no, None):
        return manifest_mod.list_files(graph_no)


def add_file(graph_no: str, path: str) -> List[str]:
    with _graph_scope(graph_no, None):
        if not graphs_mod.is_managed(graph_no):
            raise ValueError(f"not a managed graph: {graph_no}")
        return manifest_mod.add_file(graph_no, path)


def remove_file(graph_no: str, doc_no: str) -> bool:
    with _graph_scope(graph_no, None):
        return manifest_mod.remove_file(graph_no, doc_no)


def upload_files(graph_no: str, files: List[tuple[str, bytes]]) -> List[str]:
    """Persist uploaded (filename, content) pairs under the graph root and add
    them to the manifest. Returns doc_nos added or un-deleted.

    Browsers cannot hand the server a filesystem path, so the UI ships raw
    bytes; we drop them into SOURCE_DIR (the manifest root) and reuse the
    path-based add_file so doc_no derivation stays identical to the CLI.
    """
    from pathlib import Path

    from . import sources

    with _graph_scope(graph_no, None):
        if not graphs_mod.is_managed(graph_no):
            raise ValueError(f"not a managed graph: {graph_no}")
        root = manifest_mod.graph_root().resolve()
        root.mkdir(parents=True, exist_ok=True)
        added: List[str] = []
        for filename, content in files:
            name = Path(filename).name
            if not name or not sources.is_supported(Path(name)):
                raise ValueError(f"unsupported file: {filename}")
            dest = (root / name).resolve()
            if dest.parent != root:
                raise ValueError(f"invalid filename: {filename}")
            dest.write_bytes(content)
            added.extend(manifest_mod.add_file(graph_no, str(dest)))
        return added
