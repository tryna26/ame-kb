"""Persist extraction results into kg_graph_node / kg_graph_edge.

Dedup strategy (V1): graph_node_no = "type:slug(name)" so exact same-name
entities collide on the UNIQUE key and are merged via upsert. This is the
simplest seed of the V3 resolution step, at zero cost.
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
from .models import DocVersion, GraphEdge, GraphNode


@dataclass
class StoreStats:
    nodes_new: int = 0
    nodes_updated: int = 0
    edges_new: int = 0
    edges_updated: int = 0


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_unchanged(doc: Document) -> bool:
    """Return True if the doc's SHA-256 matches the last stored snapshot."""
    settings = get_settings()
    with session_scope() as session:
        existing = session.execute(
            select(DocVersion.content_hash).where(
                DocVersion.graph_no == settings.graph_no,
                DocVersion.graph_version == settings.graph_version,
                DocVersion.doc_id == doc.doc_id,
            )
        ).scalar_one_or_none()
        return existing is not None and existing == content_hash(doc.text)


def mark_processed(doc: Document) -> None:
    """Upsert the doc's content hash. Call only after a successful store so a
    failed extraction never poisons the incremental cache (llm_wiki pattern).
    """
    settings = get_settings()
    new_hash = content_hash(doc.text)
    with session_scope() as session:
        existing = session.execute(
            select(DocVersion).where(
                DocVersion.graph_no == settings.graph_no,
                DocVersion.graph_version == settings.graph_version,
                DocVersion.doc_id == doc.doc_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                DocVersion(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    doc_id=doc.doc_id,
                    content_hash=new_hash,
                )
            )
        else:
            existing.content_hash = new_hash


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
                session.add(
                    GraphNode(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_node_no=nno,
                        name=node.name,
                        type=node.type,
                        properties=node.properties or {},
                        ref=_merge_ref({}, result.doc_id, node.source),
                    )
                )
                stats.nodes_new += 1
            else:
                merged_props = dict(existing.properties or {})
                merged_props.update(node.properties or {})
                existing.properties = merged_props
                existing.ref = _merge_ref(existing.ref, result.doc_id, node.source)
                stats.nodes_updated += 1

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
                session.add(
                    GraphEdge(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_edge_no=eno,
                        source_node_no=src_no,
                        target_node_no=dst_no,
                        name=edge.label,
                        properties={"confidence": edge.confidence},
                        ref=_merge_ref({}, result.doc_id, edge.source),
                    )
                )
                stats.edges_new += 1
            else:
                merged_props = dict(existing.properties or {})
                merged_props["confidence"] = edge.confidence
                existing.properties = merged_props
                existing.ref = _merge_ref(existing.ref, result.doc_id, edge.source)
                stats.edges_updated += 1

    return stats
