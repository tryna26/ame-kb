"""Graph registry + version context (V6).

A graph is identified by a system-generated `graph_no` (graph_<8-hex>) and has
one or more integer versions. `apply_graph_context` selects a request/task-local
GraphContext so every downstream `get_settings()` sees the selected graph. The
selection uses contextvars rather than process-wide environment mutation, which
makes it safe for concurrent REST/MCP requests while keeping CLI call sites
unchanged.

Version resolution ("latest version of a graph") is deliberately fault
tolerant: it runs inside the CLI callback *before every command*, including
`init-db` which is what first creates `kg_graph`. On a fresh database the table
does not exist yet, so a failed lookup must silently fall back to the default
version instead of aborting the command (the bootstrap/chicken-and-egg case).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from .config import get_default_settings, set_graph_context
from .db import session_scope
from .models import Graph

DEFAULT_GRAPH_NO = "default"


@dataclass
class GraphInfo:
    graph_no: str
    graph_version: int
    name: str
    status: str


def new_graph_no() -> str:
    return f"graph_{uuid.uuid4().hex[:8]}"


def apply_graph_context(
    graph_no: Optional[str], graph_version: Optional[int]
) -> None:
    """Select a graph for the current request/task context.

    - graph_no None/empty -> use the environment-configured default.
    - graph_version None  -> resolve to the graph's latest ACTIVE version (fault
      tolerant: any DB/lookup error falls back to its configured version).
    """
    defaults = get_default_settings()
    active_no = graph_no or defaults.graph_no
    active_version = graph_version

    if active_version is None and active_no != DEFAULT_GRAPH_NO:
        # Only hit the DB for an explicitly named graph; the default graph keeps
        # the legacy behaviour and never triggers a kg_graph lookup (so init-db
        # works on a fresh database where kg_graph does not exist yet).
        active_version = _latest_version_safe(active_no)
    if active_version is None:
        active_version = defaults.graph_version

    set_graph_context(active_no, active_version)


def _latest_version_safe(graph_no: str) -> Optional[int]:
    """Latest ACTIVE graph version, or None if unavailable.

    Swallows the "table does not exist" / DB-unreachable cases so the callback
    never breaks init-db on a fresh database.
    """
    try:
        with session_scope() as session:
            return session.execute(
                select(func.max(Graph.graph_version)).where(
                    Graph.graph_no == graph_no,
                    Graph.status == "ACTIVE",
                )
            ).scalar_one_or_none()
    except (OperationalError, ProgrammingError, RuntimeError):
        return None


def latest_version(graph_no: str) -> Optional[int]:
    """Latest ACTIVE version (propagates real errors). None if none."""
    with session_scope() as session:
        return session.execute(
            select(func.max(Graph.graph_version)).where(
                Graph.graph_no == graph_no,
                Graph.status == "ACTIVE",
            )
        ).scalar_one_or_none()


def is_managed(graph_no: str) -> bool:
    """True if this graph_no has any kg_graph row (managed/versioned mode)."""
    with session_scope() as session:
        return (
            session.execute(
                select(Graph.id).where(Graph.graph_no == graph_no).limit(1)
            ).scalar_one_or_none()
            is not None
        )


def create_graph(name: str) -> str:
    """Register a new graph at version 1 (ACTIVE). Returns its graph_no."""
    graph_no = new_graph_no()
    with session_scope() as session:
        session.add(
            Graph(graph_no=graph_no, graph_version=1, name=name, status="ACTIVE")
        )
    return graph_no


def list_graphs() -> List[GraphInfo]:
    """One row per graph_no, reporting its latest version."""
    with session_scope() as session:
        rows = (
            session.execute(select(Graph).order_by(Graph.graph_no, Graph.graph_version))
            .scalars()
            .all()
        )
    latest_by_no: dict = {}
    for r in rows:
        cur = latest_by_no.get(r.graph_no)
        if cur is None or r.graph_version > cur.graph_version:
            latest_by_no[r.graph_no] = r
    return [
        GraphInfo(r.graph_no, r.graph_version, r.name, r.status)
        for r in latest_by_no.values()
    ]
