"""Version continuity: derive vN+1 from vN by inheriting unchanged knowledge
and re-extracting only changed/new files (V6).

build_next_version is the orchestrator:
  1. Resolve base/target versions (first ingest fills v1 in place; otherwise
     derive vN+1 and freeze vN). A half-built BUILDING version is resumed, not
     duplicated.
  2. classify manifest files against the base version's kg_doc.sha256 into
     UNCHANGED / CHANGED / NEW / REMOVED.
  3. _project the UNCHANGED slice from base to target: nodes/edges whose ref
     survives the doc filter, their kg_search_index rows (WITH embeddings, so
     nothing is re-embedded), the docs' kg_doc/kg_doc_line/kg_doc_chunk (+ their
     DOC_CHUNK index rows), kg_domain_entity, and surviving kg_entity_alias.
  4. extract CHANGED + NEW files into the target version (the only LLM calls).
  5. Flip target -> ACTIVE and base -> FROZEN.

Version projection copies the search index through the active backend's
copy_version (HybridIndex), so both MySQL (kg_search_index rows) and Redis (hash
keys carrying graph_no/version) inherit the base slice with embeddings intact --
nothing is re-embedded on either backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import extract as extract_mod
from . import ingest as ingest_mod
from . import store as store_mod
from .config import graph_context
from .db import session_scope
from .manifest import ManifestFile
from .models import (
    Doc,
    DocChunk,
    DocLine,
    DomainEntity,
    EntityAlias,
    Graph,
    GraphEdge,
    GraphNode,
)
from .searchbackend import get_index
from .searchindex import DOC_CHUNK, EDGE, NODE


@dataclass
class Classification:
    unchanged: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    new: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    @property
    def to_extract(self) -> List[str]:
        return self.changed + self.new

    @property
    def has_changes(self) -> bool:
        return bool(self.changed or self.new or self.removed)


@dataclass
class BuildResult:
    graph_no: str
    base_version: Optional[int]
    target_version: int
    classification: Classification
    projected_nodes: int = 0
    projected_edges: int = 0
    extracted_docs: int = 0
    skipped: bool = False  # True when a no-op ingest left the version untouched
    warnings: List[str] = field(default_factory=list)


def classify(manifest_hashes: Dict[str, str], base_hashes: Dict[str, str]) -> Classification:
    """Bucket manifest doc_nos against a base version's stored hashes."""
    c = Classification()
    for doc_no, h in manifest_hashes.items():
        if doc_no not in base_hashes:
            c.new.append(doc_no)
        elif base_hashes[doc_no] != h:
            c.changed.append(doc_no)
        else:
            c.unchanged.append(doc_no)
    for doc_no in base_hashes:
        if doc_no not in manifest_hashes:
            c.removed.append(doc_no)
    return c


def _base_hashes(session: Session, graph_no: str, version: int) -> Dict[str, str]:
    rows = session.execute(
        select(Doc.doc_no, Doc.sha256).where(
            Doc.graph_no == graph_no, Doc.graph_version == version
        )
    ).all()
    return {doc_no: sha for doc_no, sha in rows}


def _load_manifest_docs(
    files: List[ManifestFile],
) -> Tuple[Dict[str, "ingest_mod.Document"], Dict[str, str], List[str]]:
    """Read each manifest file into a Document + content hash.

    Files that fail to load (e.g. deleted from disk since add-file) are dropped
    with a warning; they are then absent from manifest_hashes, so classify treats
    them as REMOVED for this version rather than aborting the whole ingest.
    """
    from pathlib import Path

    from . import sources

    docs: Dict[str, "ingest_mod.Document"] = {}
    hashes: Dict[str, str] = {}
    warnings: List[str] = []
    for f in files:
        try:
            text = sources.load(Path(f.path))
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't abort
            warnings.append(f"skip {f.doc_no}: cannot load ({exc})")
            continue
        if not text.strip():
            warnings.append(f"skip {f.doc_no}: empty after normalization")
            continue
        doc = ingest_mod.Document(
            doc_id=f.doc_no,
            path=Path(f.path),
            text=text,
            origin_url=f.origin_url,
        )
        docs[f.doc_no] = doc
        hashes[f.doc_no] = store_mod.content_hash(text)
    return docs, hashes, warnings


def _filter_ref(ref: Optional[dict], keep_docs: Set[str]) -> dict:
    """Keep only the ref entries whose doc_no is in keep_docs."""
    return {
        doc_no: lines
        for doc_no, lines in (ref or {}).items()
        if doc_no in keep_docs
    }


def _copy_index_rows(
    session: Session,
    graph_no: str,
    base_v: int,
    target_v: int,
    object_type: str,
    object_nos: Set[str],
) -> None:
    """Copy search-index rows (incl. embedding) for a set of object_nos from
    base_v to target_v through the active backend, so projected objects stay
    searchable without re-embedding. Routed via HybridIndex.copy_version so the
    Redis backend (keys carry graph_no/version) is version-safe too, not just
    MySQL's kg_search_index table.
    """
    if not object_nos:
        return
    get_index().copy_version(
        graph_no,
        object_type,
        list(object_nos),
        base_v,
        target_v,
        session=session,
    )


def _project(
    session: Session,
    graph_no: str,
    base_v: int,
    target_v: int,
    unchanged_docs: Set[str],
) -> Tuple[int, int]:
    """Copy the UNCHANGED slice of vN (base) into vN+1 (target).

    Returns (nodes_copied, edges_copied). A node is copied iff its ref, filtered
    to unchanged docs, is non-empty. An edge is copied iff both endpoints were
    copied and its filtered ref is non-empty. Search-index rows (with embedding),
    docs/lines/chunks, domain entities, and surviving aliases are copied too.
    """
    # Docs + lines + chunks for unchanged docs.
    if unchanged_docs:
        docs = (
            session.execute(
                select(Doc).where(
                    Doc.graph_no == graph_no,
                    Doc.graph_version == base_v,
                    Doc.doc_no.in_(list(unchanged_docs)),
                )
            )
            .scalars()
            .all()
        )
        for d in docs:
            session.add(
                Doc(
                    graph_no=graph_no,
                    graph_version=target_v,
                    doc_no=d.doc_no,
                    path=d.path,
                    title=d.title,
                    sha256=d.sha256,
                    source_type=d.source_type,
                    origin_url=d.origin_url,
                    workspace_id=d.workspace_id,
                )
            )
        lines = (
            session.execute(
                select(DocLine).where(
                    DocLine.graph_no == graph_no,
                    DocLine.graph_version == base_v,
                    DocLine.doc_no.in_(list(unchanged_docs)),
                )
            )
            .scalars()
            .all()
        )
        for ln in lines:
            session.add(
                DocLine(
                    graph_no=graph_no,
                    graph_version=target_v,
                    doc_no=ln.doc_no,
                    line_no=ln.line_no,
                    content=ln.content,
                )
            )
        chunks = (
            session.execute(
                select(DocChunk).where(
                    DocChunk.graph_no == graph_no,
                    DocChunk.graph_version == base_v,
                    DocChunk.doc_no.in_(list(unchanged_docs)),
                )
            )
            .scalars()
            .all()
        )
        chunk_nos: Set[str] = set()
        for c in chunks:
            chunk_nos.add(c.chunk_no)
            session.add(
                DocChunk(
                    graph_no=graph_no,
                    graph_version=target_v,
                    doc_no=c.doc_no,
                    chunk_no=c.chunk_no,
                    chunk_index=c.chunk_index,
                    content=c.content,
                    origin_url=c.origin_url,
                    file_path=c.file_path,
                    line_start=c.line_start,
                    line_end=c.line_end,
                    sha256=c.sha256,
                    workspace_id=c.workspace_id,
                )
            )
        _copy_index_rows(session, graph_no, base_v, target_v, DOC_CHUNK, chunk_nos)

    # Nodes: keep those with surviving ref.
    base_nodes = (
        session.execute(
            select(GraphNode).where(
                GraphNode.graph_no == graph_no,
                GraphNode.graph_version == base_v,
                GraphNode.deleted == 0,
            )
        )
        .scalars()
        .all()
    )
    kept_node_nos: Set[str] = set()
    for n in base_nodes:
        new_ref = _filter_ref(n.ref, unchanged_docs)
        if not new_ref:
            continue
        kept_node_nos.add(n.graph_node_no)
        session.add(
            GraphNode(
                graph_no=graph_no,
                graph_version=target_v,
                graph_node_no=n.graph_node_no,
                name=n.name,
                type=n.type,
                description=n.description,
                properties=n.properties,
                ref=new_ref,
                deleted=0,
            )
        )

    # Edges: both endpoints kept AND surviving ref.
    base_edges = (
        session.execute(
            select(GraphEdge).where(
                GraphEdge.graph_no == graph_no,
                GraphEdge.graph_version == base_v,
                GraphEdge.deleted == 0,
            )
        )
        .scalars()
        .all()
    )
    kept_edge_nos: Set[str] = set()
    for e in base_edges:
        if e.source_node_no not in kept_node_nos or e.target_node_no not in kept_node_nos:
            continue
        new_ref = _filter_ref(e.ref, unchanged_docs)
        if not new_ref:
            continue
        kept_edge_nos.add(e.graph_edge_no)
        session.add(
            GraphEdge(
                graph_no=graph_no,
                graph_version=target_v,
                graph_edge_no=e.graph_edge_no,
                source_node_no=e.source_node_no,
                target_node_no=e.target_node_no,
                name=e.name,
                description=e.description,
                properties=e.properties,
                ref=new_ref,
                deleted=0,
            )
        )

    _copy_index_rows(session, graph_no, base_v, target_v, NODE, kept_node_nos)
    _copy_index_rows(session, graph_no, base_v, target_v, EDGE, kept_edge_nos)

    # Domain entities (whole schema layer) copied verbatim.
    entities = (
        session.execute(
            select(DomainEntity).where(
                DomainEntity.graph_no == graph_no,
                DomainEntity.graph_version == base_v,
                DomainEntity.deleted == 0,
            )
        )
        .scalars()
        .all()
    )
    for de in entities:
        session.add(
            DomainEntity(
                entity_name=de.entity_name,
                cn_name=de.cn_name,
                entity_type=de.entity_type,
                description=de.description,
                core_schema=de.core_schema,
                graph_no=graph_no,
                graph_version=target_v,
                deleted=0,
            )
        )

    # Aliases pointing at a surviving node.
    aliases = (
        session.execute(
            select(EntityAlias).where(
                EntityAlias.graph_no == graph_no,
                EntityAlias.graph_version == base_v,
                EntityAlias.canonical_node_no.in_(list(kept_node_nos or {""})),
            )
        )
        .scalars()
        .all()
    )
    for a in aliases:
        session.add(
            EntityAlias(
                graph_no=graph_no,
                graph_version=target_v,
                canonical_node_no=a.canonical_node_no,
                alias=a.alias,
                source=a.source,
            )
        )

    return len(kept_node_nos), len(kept_edge_nos)


def _has_docs(session: Session, graph_no: str, version: int) -> bool:
    return (
        session.execute(
            select(Doc.id)
            .where(Doc.graph_no == graph_no, Doc.graph_version == version)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _resolve_versions(session: Session, graph_no: str) -> Tuple[Optional[int], int, bool]:
    """Return (base_version, target_version, is_resume).

    - A BUILDING row means a prior ingest was interrupted -> resume it.
    - No rows / empty latest version -> fill v1 in place (base=None).
    - Populated latest version vN -> derive vN+1 (base=vN).
    """
    building = session.execute(
        select(Graph.graph_version).where(
            Graph.graph_no == graph_no, Graph.status == "BUILDING"
        )
    ).scalar_one_or_none()
    if building is not None:
        base = session.execute(
            select(func.max(Graph.graph_version)).where(
                Graph.graph_no == graph_no,
                Graph.graph_version < building,
            )
        ).scalar_one_or_none()
        return base, building, True

    latest = session.execute(
        select(func.max(Graph.graph_version)).where(Graph.graph_no == graph_no)
    ).scalar_one_or_none()
    if latest is None:
        # Unregistered graph_no under a managed flow: start at v1.
        return None, 1, False
    if not _has_docs(session, graph_no, latest):
        return None, latest, False  # fill in place
    return latest, latest + 1, False


def build_next_version(
    graph_no: str,
    files: List[ManifestFile],
    *,
    force: bool = False,
    allow_empty: bool = False,
    dry_run: bool = False,
) -> BuildResult:
    """Assemble the next version of `graph_no` from its manifest `files`."""
    docs, manifest_hashes, load_warnings = _load_manifest_docs(files)

    with session_scope() as session:
        base_v, target_v, is_resume = _resolve_versions(session, graph_no)
        base_hashes = _base_hashes(session, graph_no, base_v) if base_v else {}

    cls = classify(manifest_hashes, base_hashes)
    if force:
        cls.changed = sorted(set(cls.changed) | set(cls.unchanged))
        cls.unchanged = []

    result = BuildResult(
        graph_no=graph_no,
        base_version=base_v,
        target_version=target_v,
        classification=cls,
        warnings=load_warnings,
    )

    if dry_run:
        return result

    # No-op guard: nothing changed and this would be a fresh derived version.
    is_new_version = base_v is not None and target_v != base_v
    if is_new_version and not is_resume and not cls.has_changes and not allow_empty:
        result.skipped = True
        return result

    # Mark target BUILDING (create the row when deriving; reuse when resuming or
    # filling in place).
    with session_scope() as session:
        row = session.execute(
            select(Graph).where(
                Graph.graph_no == graph_no, Graph.graph_version == target_v
            )
        ).scalar_one_or_none()
        if row is None:
            name = _graph_name(session, graph_no)
            session.add(
                Graph(
                    graph_no=graph_no,
                    graph_version=target_v,
                    name=name,
                    status="BUILDING",
                )
            )
        else:
            row.status = "BUILDING"

    # Project the unchanged slice (only when deriving a new version and not
    # already projected by a previous, interrupted run).
    if is_new_version:
        with session_scope() as session:
            already = _has_docs(session, graph_no, target_v)
            if not already:
                pn, pe = _project(
                    session, graph_no, base_v, target_v, set(cls.unchanged)
                )
                result.projected_nodes = pn
                result.projected_edges = pe

    # Extract changed + new files into the target version.
    with graph_context(graph_no, target_v):
        for doc_no in cls.to_extract:
            doc = docs.get(doc_no)
            if doc is None:
                continue
            if not force and store_mod.is_unchanged(doc):
                continue  # already persisted by an interrupted run (resume)
            ext = extract_mod.extract(doc)
            store_mod.store(ext)
            ingest_mod.persist_doc(doc)
            result.extracted_docs += 1

    # Finalize: target ACTIVE, base FROZEN.
    with session_scope() as session:
        target_row = session.execute(
            select(Graph).where(
                Graph.graph_no == graph_no, Graph.graph_version == target_v
            )
        ).scalar_one_or_none()
        if target_row is not None:
            target_row.status = "ACTIVE"
        if is_new_version:
            base_row = session.execute(
                select(Graph).where(
                    Graph.graph_no == graph_no, Graph.graph_version == base_v
                )
            ).scalar_one_or_none()
            if base_row is not None:
                base_row.status = "FROZEN"

    return result


def _graph_name(session: Session, graph_no: str) -> str:
    return (
        session.execute(
            select(Graph.name)
            .where(Graph.graph_no == graph_no)
            .order_by(Graph.graph_version)
            .limit(1)
        ).scalar_one_or_none()
        or ""
    )
