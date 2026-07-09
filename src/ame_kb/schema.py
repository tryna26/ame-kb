"""Node/edge type schema: fixed seed (V1) + semi-dynamic DB layer (V5).

The seed types below are the source of truth on a fresh graph and the offline
fallback when the DB is unavailable. From V5 the *active* schema is the seed
UNION any types registered into kg_domain_entity, so the LLM can propose new
types that get persisted and honoured on subsequent runs (gated by
SCHEMA_DYNAMIC).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .config import get_settings
from .db import session_scope
from .models import DomainEntity


@dataclass(frozen=True)
class NodeType:
    name: str
    cn_name: str
    description: str
    fields: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class EdgeType:
    name: str
    cn_name: str
    description: str
    # allowed (source_type, target_type) pairs; "*" means any node type
    endpoints: List[Tuple[str, str]] = field(default_factory=list)


SEED_NODE_TYPES: List[NodeType] = [
    NodeType("Person", "人物", "具名的个人", ["title"]),
    NodeType("Organization", "组织", "公司/团队/机构", ["industry"]),
    NodeType("Project", "项目", "有边界的工作项或产品", ["status"]),
    NodeType("Document", "文档", "一篇文档/文章/报告", ["doc_type"]),
    NodeType("Concept", "概念", "抽象概念/术语/主题", []),
]

SEED_EDGE_TYPES: List[EdgeType] = [
    EdgeType("works_for", "任职于", "人物任职于某组织", [("Person", "Organization")]),
    EdgeType("authored", "撰写", "人物撰写了某文档", [("Person", "Document")]),
    EdgeType("part_of", "属于", "某实体从属于某项目", [("*", "Project")]),
    EdgeType("mentions", "提及", "文档提及某概念", [("Document", "Concept")]),
    EdgeType("related_to", "相关", "两个实体存在关联", [("*", "*")]),
]

# Backward-compat aliases: the seed view (used by first-init and offline tests).
NODE_TYPES = SEED_NODE_TYPES
EDGE_TYPES = SEED_EDGE_TYPES
NODE_TYPE_NAMES = {t.name for t in SEED_NODE_TYPES}
EDGE_TYPE_NAMES = {t.name for t in SEED_EDGE_TYPES}
NODE_FIELDS: Dict[str, List[str]] = {t.name: list(t.fields) for t in SEED_NODE_TYPES}


def _parse_node_fields(core_schema: str) -> List[str]:
    try:
        rows = json.loads(core_schema or "[]")
        return [r["name"] for r in rows if isinstance(r, dict) and r.get("name")]
    except (ValueError, TypeError):
        return []


def _parse_edge_endpoints(core_schema: str) -> List[Tuple[str, str]]:
    try:
        rows = json.loads(core_schema or "[]")
        out: List[Tuple[str, str]] = []
        for r in rows:
            for pair in (r or {}).get("endpoints", []):
                if len(pair) == 2:
                    out.append((pair[0], pair[1]))
        return out or [("*", "*")]
    except (ValueError, TypeError):
        return [("*", "*")]


def load_types_from_db() -> Tuple[List[NodeType], List[EdgeType]]:
    """Return (node_types, edge_types) = seed UNION types registered in the DB.

    Falls back to the seed lists when the DB is unavailable, so offline callers
    (and a fresh graph) still work.
    """
    nodes: Dict[str, NodeType] = {t.name: t for t in SEED_NODE_TYPES}
    edges: Dict[str, EdgeType] = {t.name: t for t in SEED_EDGE_TYPES}
    settings = get_settings()
    try:
        with session_scope() as session:
            rows = (
                session.execute(
                    select(DomainEntity).where(
                        DomainEntity.graph_no == settings.graph_no,
                        DomainEntity.graph_version == settings.graph_version,
                        DomainEntity.deleted == 0,
                    )
                )
                .scalars()
                .all()
            )
            for r in rows:
                if r.entity_type == "Node" and r.entity_name not in nodes:
                    nodes[r.entity_name] = NodeType(
                        r.entity_name,
                        r.cn_name or r.entity_name,
                        r.description or "",
                        _parse_node_fields(r.core_schema),
                    )
                elif r.entity_type == "Relation" and r.entity_name not in edges:
                    edges[r.entity_name] = EdgeType(
                        r.entity_name,
                        r.cn_name or r.entity_name,
                        r.description or "",
                        _parse_edge_endpoints(r.core_schema),
                    )
    except (OperationalError, RuntimeError):
        # DB unreachable/unconfigured -> seed fallback. Real schema/programming
        # errors (bad rows, missing table) are left to propagate rather than
        # being silently masked as "a few missing types".
        return list(SEED_NODE_TYPES), list(SEED_EDGE_TYPES)
    return list(nodes.values()), list(edges.values())


def active_node_type_names() -> set:
    nodes, _ = load_types_from_db()
    return {t.name for t in nodes}


def active_edge_type_names() -> set:
    _, edges = load_types_from_db()
    return {t.name for t in edges}


def edge_endpoints_ok(
    label: str,
    source_type: str,
    target_type: str,
    edge_types: List[EdgeType] = None,
) -> bool:
    # `edge_types` lets a hot loop (extract.validate) pass a once-loaded schema
    # instead of re-querying the DB for every edge. Falls back to a fresh load.
    edges = edge_types if edge_types is not None else load_types_from_db()[1]
    edge = {e.name: e for e in edges}.get(label)
    if edge is None:
        return False
    for src, dst in edge.endpoints:
        if (src in ("*", source_type)) and (dst in ("*", target_type)):
            return True
    return False


def schema_prompt_block() -> str:
    """Human/LLM-readable description of the active schema for the prompt."""
    node_types, edge_types = load_types_from_db()
    lines = ["## 允许的节点类型 (type 只能取以下之一)"]
    for t in node_types:
        fields_str = ", ".join(t.fields) if t.fields else "(无字段)"
        lines.append(f"- {t.name} ({t.cn_name}): {t.description} | 字段: {fields_str}")
    lines.append("")
    lines.append("## 允许的关系类型 (label 只能取以下之一)")
    for e in edge_types:
        ep = ", ".join(f"{s}->{d}" for s, d in e.endpoints)
        lines.append(f"- {e.name} ({e.cn_name}): {e.description} | 端点: {ep}")
    return "\n".join(lines)


def register_type(
    entity_type: str,
    name: str,
    *,
    cn_name: str = "",
    description: str = "",
    fields: List[str] = None,
    endpoints: List[Tuple[str, str]] = None,
    session=None,
) -> bool:
    """Insert a node/edge type into kg_domain_entity if absent. Idempotent.

    entity_type is "Node" or "Relation". Returns True if a row was inserted.
    """
    if entity_type == "Node":
        core = json.dumps([{"name": f} for f in (fields or [])], ensure_ascii=False)
    else:
        core = json.dumps(
            [{"endpoints": [list(p) for p in (endpoints or [("*", "*")])]}],
            ensure_ascii=False,
        )

    def _do(sess) -> bool:
        settings = get_settings()
        exists = sess.execute(
            select(DomainEntity.id).where(
                DomainEntity.entity_name == name,
                DomainEntity.graph_no == settings.graph_no,
                DomainEntity.graph_version == settings.graph_version,
            )
        ).scalar_one_or_none()
        if exists is not None:
            return False
        sess.add(
            DomainEntity(
                entity_name=name,
                cn_name=cn_name or name,
                entity_type=entity_type,
                description=description,
                core_schema=core,
                graph_no=settings.graph_no,
                graph_version=settings.graph_version,
            )
        )
        return True

    if session is not None:
        return _do(session)
    with session_scope() as own:
        return _do(own)


def seed_schema() -> int:
    """Insert seed node/edge type definitions into kg_domain_entity. Idempotent.

    Returns the number of newly inserted rows.
    """
    settings = get_settings()
    inserted = 0
    with session_scope() as session:
        existing = set(
            session.execute(
                select(DomainEntity.entity_name).where(
                    DomainEntity.graph_no == settings.graph_no,
                    DomainEntity.graph_version == settings.graph_version,
                )
            )
            .scalars()
            .all()
        )
        for t in SEED_NODE_TYPES:
            if t.name in existing:
                continue
            session.add(
                DomainEntity(
                    entity_name=t.name,
                    cn_name=t.cn_name,
                    entity_type="Node",
                    description=t.description,
                    core_schema=json.dumps(
                        [{"name": f} for f in t.fields], ensure_ascii=False
                    ),
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                )
            )
            inserted += 1
        for e in SEED_EDGE_TYPES:
            if e.name in existing:
                continue
            session.add(
                DomainEntity(
                    entity_name=e.name,
                    cn_name=e.cn_name,
                    entity_type="Relation",
                    description=e.description,
                    core_schema=json.dumps(
                        [{"endpoints": list(e.endpoints)}], ensure_ascii=False
                    ),
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                )
            )
            inserted += 1
    return inserted
