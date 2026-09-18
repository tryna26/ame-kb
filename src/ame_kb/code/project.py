"""Project a CodeGraph onto the ontology three tables (Phase 1).

Code symbols and packages become ontology nodes in the Asset/Implementation
namespace (kg_graph_node structural row + kg_domain_entity ontology row), joined
by graph_node_no. Structural code relationships become edges with
confidence=EXTRACTED (source-of-truth, versus LLM's INFERRED).

This does NOT reuse store.store(): that builds edges from a single document's
local name map and would drop the cross-file import edges that make a code graph
useful. Instead we project all nodes first (global id -> node_no map), then all
edges. The file layer is collapsed: files are not ontology nodes (they stay as
provenance in each symbol's ref); a package->symbol `contains` edge is
synthesised so symbols stay attached to their package.

Node identity uses the package-qualified name (GNode.id) so distinct symbols
that happen to share a bare name across packages never merge. Cross-source
fusion with document-extracted entities is delegated to V5 resolve (vector +
LLM), not to bare-name node_no collision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import GraphEdge, GraphNode
from ..searchindex import NODE, build_searchable_text, upsert_search_index
from ..store import STRUCTURAL_ROLE, _upsert_domain_entity, edge_no, node_no
from .codegraph import CodeGraph, EdgeKind, GNode, NodeKind

# CodeGraph node kinds that become ontology nodes, all as Asset/Implementation
# (static artifacts in the value chain). file/repository are structural-only.
_ONTOLOGY_KINDS = {
    NodeKind.PACKAGE,
    NodeKind.FUNCTION,
    NodeKind.METHOD,
    NodeKind.STRUCT,
    NodeKind.INTERFACE,
    NodeKind.TYPE_ALIAS,
}
_SYMBOL_KINDS = {
    NodeKind.FUNCTION,
    NodeKind.METHOD,
    NodeKind.STRUCT,
    NodeKind.INTERFACE,
    NodeKind.TYPE_ALIAS,
}

# CodeGraph edge kind -> ontology edge label. Contains is handled by synthesised
# package->symbol edges, so the tree's Contains rows (which touch file/repo) are
# not projected directly.
_EDGE_LABEL = {
    EdgeKind.IMPORT: "imports",
    EdgeKind.SUBPACKAGE: "subpackage",
}
CODE_CONFIDENCE = "EXTRACTED"


@dataclass
class ProjectStats:
    nodes_new: int = 0
    nodes_updated: int = 0
    edges_new: int = 0
    edges_updated: int = 0


def _ontology_of(gn: GNode) -> Optional[Tuple[str, Optional[str]]]:
    if gn.kind in _ONTOLOGY_KINDS:
        return ("Asset", "Implementation")
    return None


def _code_node_no(gn: GNode) -> str:
    # Qualified id keeps distinct symbols distinct across packages.
    return node_no("Asset", "Implementation", gn.id)


def _description_of(gn: GNode) -> Optional[str]:
    return (gn.summary or gn.doc or "").strip() or None


def _properties_of(gn: GNode) -> Dict:
    props: Dict[str, object] = {"kind": gn.kind.value}
    if gn.signature:
        props["signature"] = gn.signature
    if gn.package:
        props["package"] = gn.package
    if gn.file:
        props["file"] = gn.file
    if gn.line:
        props["line"] = gn.line
    return props


def project_code_graph(graph: CodeGraph) -> ProjectStats:
    settings = get_settings()
    stats = ProjectStats()

    with session_scope() as session:
        id_to_no: Dict[str, str] = {}
        pkg_id_to_no: Dict[str, str] = {}
        index_entries: List[Dict] = []

        # ── Pass 1: nodes ────────────────────────────────────────────────────
        for gn in graph.nodes:
            if _ontology_of(gn) is None:
                continue
            nno = _code_node_no(gn)
            id_to_no[gn.id] = nno
            if gn.kind == NodeKind.PACKAGE:
                pkg_id_to_no[gn.id] = nno

            props = _properties_of(gn)
            description = _description_of(gn)
            existing = session.execute(
                select(GraphNode).where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.graph_node_no == nno,
                )
            ).scalar_one_or_none()
            ref = {gn.file: [f"{gn.line}-{gn.end_line}"]} if gn.file and gn.line else {}
            if existing is None:
                session.add(
                    GraphNode(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_node_no=nno,
                        name=gn.id,
                        type=STRUCTURAL_ROLE,
                        description=description,
                        properties=props,
                        ref=ref,
                    )
                )
                stats.nodes_new += 1
            else:
                existing.name = gn.id
                merged = dict(existing.properties or {})
                merged.update(props)
                existing.properties = merged
                if description:
                    existing.description = description
                if ref:
                    existing.ref = ref
                existing.deleted = 0
                stats.nodes_updated += 1

            _upsert_domain_entity(
                session,
                settings.graph_no,
                settings.graph_version,
                nno,
                gn.id,
                "Asset",
                "Implementation",
                props,
            )
            index_entries.append(
                {
                    "object_type": NODE,
                    "object_no": nno,
                    "searchable_text": build_searchable_text(gn.id, description, props),
                }
            )

        # ── Pass 2: edges ────────────────────────────────────────────────────
        # Synthesised package -> symbol containment (file layer collapsed).
        synthesized: List[Tuple[str, str, str]] = []
        for gn in graph.nodes:
            if gn.kind in _SYMBOL_KINDS and gn.package in pkg_id_to_no:
                src = pkg_id_to_no[gn.package]
                dst = id_to_no.get(gn.id)
                if dst:
                    synthesized.append((src, "contains", dst))

        # Import / SubPackage edges between projected nodes.
        for e in graph.edges:
            label = _EDGE_LABEL.get(e.kind)
            if label is None:
                continue
            src = id_to_no.get(e.from_)
            dst = id_to_no.get(e.to)
            if src is None or dst is None:
                continue
            synthesized.append((src, label, dst))

        for src_no, label, dst_no in synthesized:
            eno = edge_no(src_no, label, dst_no)
            existing = session.execute(
                select(GraphEdge).where(
                    GraphEdge.graph_no == settings.graph_no,
                    GraphEdge.graph_version == settings.graph_version,
                    GraphEdge.graph_edge_no == eno,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    GraphEdge(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_edge_no=eno,
                        source_node_no=src_no,
                        target_node_no=dst_no,
                        name=label,
                        properties={"confidence": CODE_CONFIDENCE},
                    )
                )
                stats.edges_new += 1
            else:
                merged = dict(existing.properties or {})
                merged["confidence"] = CODE_CONFIDENCE
                existing.properties = merged
                existing.deleted = 0
                stats.edges_updated += 1

        upsert_search_index(session, index_entries)

    return stats
