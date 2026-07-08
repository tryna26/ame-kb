"""Persist extraction results into kg_graph_node / kg_graph_edge.

Dedup strategy (V1): graph_node_no = "type:slug(name)" so exact same-name
entities collide on the UNIQUE key and are merged via upsert. This is the
simplest seed of the V3 resolution step, at zero cost.

V3: nodes/edges also carry a `description`; after upsert we refresh the
kg_search_index rows (searchable_text + embedding) so hybrid recall stays in
sync. Incremental hashing now lives on kg_doc.sha256 (see ingest.persist_doc).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .extract import ExtractionResult
from .ingest import Document
from .models import Doc, GraphEdge, GraphNode
from .searchindex import EDGE, NODE, build_searchable_text, upsert_search_index


@dataclass
class StoreStats:
    nodes_new: int = 0
    nodes_updated: int = 0
    edges_new: int = 0
    edges_updated: int = 0


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_unchanged(doc: Document) -> bool:
    """Return True if the doc's SHA-256 matches the last stored kg_doc snapshot."""
    settings = get_settings()
    with session_scope() as session:
        existing = session.execute(
            select(Doc.sha256).where(
                Doc.graph_no == settings.graph_no,
                Doc.graph_version == settings.graph_version,
                Doc.doc_no == doc.doc_id,
            )
        ).scalar_one_or_none()
        return existing is not None and existing == content_hash(doc.text)


def slug(name: str) -> str:
    norm = unicodedata.normalize("NFKC", name).strip().lower()
    norm = re.sub(r"\s+", "-", norm)
    norm = re.sub(r"[^0-9a-z\u4e00-\u9fff\-]", "", norm)
    return norm or "unnamed"


def node_no(type_: str, name: str) -> str:
    return f"{type_}:{slug(name)}"


def edge_no(source_no: str, label: str, target_no: str) -> str:
    key = f"{source_no}|{label}|{target_no}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _merge_ref(existing: Dict, doc_id: str, lines: List[str]) -> Dict:
    ref = dict(existing or {})
    merged = sorted(set(ref.get(doc_id, [])) | set(lines or []))
    ref[doc_id] = merged
    return ref


def store(result: ExtractionResult) -> StoreStats:
    settings = get_settings()
    stats = StoreStats()
    with session_scope() as session:
        name_to_no: Dict[str, str] = {}
        index_entries: List[Dict] = []

        for node in result.nodes:
            nno = node_no(node.type, node.name)
            name_to_no[node.name] = nno
            existing = session.execute(
                select(GraphNode).where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.graph_node_no == nno,
                )
            ).scalar_one_or_none()
            if existing is None:
                merged_props = node.properties or {}
                description = node.description or None
                session.add(
                    GraphNode(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_node_no=nno,
                        name=node.name,
                        type=node.type,
                        description=description,
                        properties=merged_props,
                        ref=_merge_ref({}, result.doc_id, node.source),
                    )
                )
                stats.nodes_new += 1
            else:
                merged_props = dict(existing.properties or {})
                merged_props.update(node.properties or {})
                existing.properties = merged_props
                if node.description:
                    existing.description = node.description
                description = existing.description
                existing.ref = _merge_ref(existing.ref, result.doc_id, node.source)
                stats.nodes_updated += 1
            index_entries.append(
                {
                    "object_type": NODE,
                    "object_no": nno,
                    "searchable_text": build_searchable_text(
                        node.name, description, merged_props
                    ),
                }
            )

        for edge in result.edges:
            src_no = name_to_no.get(edge.source_name)
            dst_no = name_to_no.get(edge.target_name)
            if src_no is None or dst_no is None:
                continue
            eno = edge_no(src_no, edge.label, dst_no)
            existing = session.execute(
                select(GraphEdge).where(
                    GraphEdge.graph_no == settings.graph_no,
                    GraphEdge.graph_version == settings.graph_version,
                    GraphEdge.graph_edge_no == eno,
                )
            ).scalar_one_or_none()
            if existing is None:
                merged_props = {"confidence": edge.confidence}
                description = edge.description or None
                session.add(
                    GraphEdge(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_edge_no=eno,
                        source_node_no=src_no,
                        target_node_no=dst_no,
                        name=edge.label,
                        description=description,
                        properties=merged_props,
                        ref=_merge_ref({}, result.doc_id, edge.source),
                    )
                )
                stats.edges_new += 1
            else:
                merged_props = dict(existing.properties or {})
                merged_props["confidence"] = edge.confidence
                existing.properties = merged_props
                if edge.description:
                    existing.description = edge.description
                description = existing.description
                existing.ref = _merge_ref(existing.ref, result.doc_id, edge.source)
                stats.edges_updated += 1
            index_entries.append(
                {
                    "object_type": EDGE,
                    "object_no": eno,
                    "searchable_text": build_searchable_text(
                        edge.label, description, merged_props
                    ),
                }
            )

        upsert_search_index(session, index_entries)

    return stats

