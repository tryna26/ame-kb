"""File manifest management (V6).

The manifest (kg_graph_file) is the user-maintained, cross-version list of
source files for a managed graph. It is NOT versioned: add/remove edit the same
evolving list, and each ingest snapshots the live rows into a new version.

doc_no normalization: a file's business key is its path relative to the graph
root (default SOURCE_DIR). Both `add-file <root>` (directory expand) and
`add-file <root>/sub/a.md` resolve each file against the same root, so the same
file always maps to the same doc_no regardless of how it was added. Files
outside the root fall back to their absolute path (stable, if less tidy).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select

from . import sources
from .config import get_settings
from .db import session_scope
from .models import GraphFile


@dataclass
class ManifestFile:
    doc_no: str
    path: str
    source_type: str
    origin_url: str


def graph_root() -> Path:
    return Path(get_settings().source_dir).expanduser().resolve()


def doc_no_for(path: Path, root: Optional[Path] = None) -> str:
    """Stable business key for a file path, relative to the graph root."""
    root = root or graph_root()
    resolved = path.expanduser().resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


def expand_paths(path: str, root: Optional[Path] = None) -> List[ManifestFile]:
    """Expand a file or directory into supported ManifestFile entries."""
    root = root or graph_root()
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")
    out: List[ManifestFile] = []
    candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
    for f in candidates:
        if not f.is_file() or not sources.is_supported(f):
            continue
        out.append(
            ManifestFile(
                doc_no=doc_no_for(f, root),
                path=str(f),
                source_type=f.suffix.lower().lstrip("."),
                origin_url="",
            )
        )
    return out


def add_file(graph_no: str, path: str) -> List[str]:
    """Add a file or directory to the manifest. Returns the added doc_nos.

    Re-adding a previously removed file un-deletes it (and refreshes its path).
    """
    files = expand_paths(path)
    added: List[str] = []
    with session_scope() as session:
        for f in files:
            existing = session.execute(
                select(GraphFile).where(
                    GraphFile.graph_no == graph_no,
                    GraphFile.doc_no == f.doc_no,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    GraphFile(
                        graph_no=graph_no,
                        doc_no=f.doc_no,
                        path=f.path,
                        source_type=f.source_type,
                        origin_url=f.origin_url,
                        deleted=0,
                    )
                )
                added.append(f.doc_no)
            else:
                existing.path = f.path
                existing.source_type = f.source_type
                existing.origin_url = f.origin_url
                if existing.deleted:
                    existing.deleted = 0
                    added.append(f.doc_no)
    return added


def remove_file(graph_no: str, doc_no: str) -> bool:
    """Soft-delete a manifest entry by doc_no. Returns True if a row changed."""
    with session_scope() as session:
        row = session.execute(
            select(GraphFile).where(
                GraphFile.graph_no == graph_no,
                GraphFile.doc_no == doc_no,
                GraphFile.deleted == 0,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.deleted = 1
        return True


def list_files(graph_no: str) -> List[ManifestFile]:
    """Live (deleted=0) manifest entries for a graph."""
    with session_scope() as session:
        rows = (
            session.execute(
                select(GraphFile)
                .where(GraphFile.graph_no == graph_no, GraphFile.deleted == 0)
                .order_by(GraphFile.doc_no)
            )
            .scalars()
            .all()
        )
        return [
            ManifestFile(r.doc_no, r.path, r.source_type, r.origin_url) for r in rows
        ]
