"""Persist extraction results into kg_graph_node / kg_graph_edge / kg_domain_entity.

Two-layer model: kg_graph_node is the structural layer (type always "ENTITY"),
kg_domain_entity is the domain instance layer (type = ontology class
Asset/Relation/Event/Behavior, entity_spec = Asset archetype). Both rows share
graph_node_no as the join key.

Dedup strategy: graph_node_no = "type:spec:slug(name)" for Asset nodes (spec is
the archetype) and "type:slug(name)" otherwise, so the same real entity in the
same ontology layer collides on the UNIQUE key and is merged via upsert. This is
the simplest seed of the V3 resolution step, at zero cost.

V3: nodes/edges also carry a `description`; after upsert we refresh the
kg_search_index rows (searchable_text + embedding) so hybrid recall stays in
sync. Incremental hashing now lives on kg_doc.sha256 (see ingest.persist_doc).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .extract import ExtractionResult
from .ingest import Document
from .models import Doc, DomainEntity, GraphEdge, GraphNode
from .searchindex import EDGE, NODE, build_searchable_text, upsert_search_index

# Structural role written to kg_graph_node.type; the ontology class lives in the
# domain layer (kg_domain_entity.type).
STRUCTURAL_ROLE = "ENTITY"


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


def node_no(entity_type: str, entity_spec: Optional[str], name: str) -> str:
    prefix = f"{entity_type}:{entity_spec}" if entity_spec else entity_type
    return f"{prefix}:{slug(name)}"


def edge_no(source_no: str, label: str, target_no: str) -> str:
    key = f"{source_no}|{label}|{target_no}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _upsert_domain_entity(
    session: Session,
    graph_no: str,
    graph_version: int,
    nno: str,
    name: str,
    entity_type: str,
    entity_spec: Optional[str],
    properties: Optional[Dict],
) -> None:
    """Mirror a structural node into the domain instance layer (same node_no)."""
    existing = session.execute(
        select(DomainEntity).where(
            DomainEntity.graph_no == graph_no,
            DomainEntity.graph_version == graph_version,
            DomainEntity.graph_node_no == nno,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DomainEntity(
                graph_no=graph_no,
                graph_version=graph_version,
                graph_node_no=nno,
                name=name,
                type=entity_type,
                entity_spec=entity_spec,
                properties=properties or {},
            )
        )
    else:
        existing.name = name
        existing.type = entity_type
        existing.entity_spec = entity_spec
        existing.properties = properties or {}
        existing.deleted = 0


def load_domain_map(
    session: Session,
    graph_no: str,
    graph_version: int,
    node_nos: List[str],
) -> Dict[str, Tuple[str, Optional[str]]]:
    """Batch-load {graph_node_no: (type, entity_spec)} for the given nodes.

    Used by read paths (recall/query) to surface the ontology class + archetype,
    since kg_graph_node.type is now the structural role ("ENTITY").
    """
    if not node_nos:
        return {}
    rows = session.execute(
        select(
            DomainEntity.graph_node_no, DomainEntity.type, DomainEntity.entity_spec
        ).where(
            DomainEntity.graph_no == graph_no,
            DomainEntity.graph_version == graph_version,
            DomainEntity.graph_node_no.in_(list(node_nos)),
            DomainEntity.deleted == 0,
        )
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def _merge_ref(existing: Dict, doc_id: str, lines: List[str]) -> Dict:
    ref = dict(existing or {})
    merged = sorted(set(ref.get(doc_id, [])) | set(lines or []))
    ref[doc_id] = merged
    return ref


def merge_ref_maps(a: Dict, b: Dict) -> Dict:
    """Union two ref maps ({doc_id: [line, ...]}) doc by doc. Used by V5 fusion
    to fold a merged-away node's provenance into the survivor."""
    out = dict(a or {})
    for doc_id, lines in (b or {}).items():
        out[doc_id] = sorted(set(out.get(doc_id, [])) | set(lines or []))
    return out


def merge_props(winner: Dict, loser: Dict) -> Dict:
    """Field-union of two property maps: winner wins on conflict, loser fills
    keys the winner is missing (V5 fusion)."""
    out = dict(loser or {})
    out.update(winner or {})
    return out


def store(result: ExtractionResult) -> StoreStats:
    settings = get_settings()
    stats = StoreStats()
    with session_scope() as session:
        name_to_no: Dict[str, str] = {}
        index_entries: List[Dict] = []

        for node in result.nodes:
            nno = node_no(node.entity_type, node.entity_spec, node.name)
            existing = session.execute(
                select(GraphNode).where(
                    GraphNode.graph_no == settings.graph_no,
                    GraphNode.graph_version == settings.graph_version,
                    GraphNode.graph_node_no == nno,
                )
            ).scalar_one_or_none()
            # A node_no merged away (soft-deleted) still owns the unique key, so
            # we can't INSERT a fresh row for it; re-ingesting must NOT resurrect
            # it in the search index. Skip it (and leave it out of name_to_no so
            # edges pointing at the dead node are dropped too). Its mentions stay
            # reachable via the survivor's alias-augmented searchable_text.
            if existing is not None and existing.deleted:
                continue
            name_to_no[node.name] = nno
            if existing is None:
                merged_props = node.properties or {}
                description = node.description or None
                session.add(
                    GraphNode(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        graph_node_no=nno,
                        name=node.name,
                        type=STRUCTURAL_ROLE,
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
            _upsert_domain_entity(
                session,
                settings.graph_no,
                settings.graph_version,
                nno,
                node.name,
                node.entity_type,
                node.entity_spec,
                merged_props,
            )
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

