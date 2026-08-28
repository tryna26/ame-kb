"""V1 fixed schema: hand-written node types, edge types, and fields.

This is intentionally small and static. V2+ will make it semi/fully dynamic.
The schema is both the source of truth for the extraction prompt (what the LLM
is allowed to emit) and the seed for the kg_domain_entity table.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from sqlalchemy import inspect, select

from .config import get_settings
from .db import acquire_graph_write_lock, get_engine, session_scope
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


NODE_TYPES: List[NodeType] = [
    NodeType("Person", "人物", "具名的个人", ["title"]),
    NodeType("Organization", "组织", "公司/团队/机构", ["industry"]),
    NodeType("Project", "项目", "有边界的工作项或产品", ["status"]),
    NodeType("Document", "文档", "一篇文档/文章/报告", ["doc_type"]),
    NodeType("Concept", "概念", "抽象概念/术语/主题", []),
]

EDGE_TYPES: List[EdgeType] = [
    EdgeType("works_for", "任职于", "人物任职于某组织", [("Person", "Organization")]),
    EdgeType("authored", "撰写", "人物撰写了某文档", [("Person", "Document")]),
    EdgeType("part_of", "属于", "某实体从属于某项目", [("*", "Project")]),
    EdgeType("mentions", "提及", "文档提及某概念", [("Document", "Concept")]),
    EdgeType("related_to", "相关", "两个实体存在关联", [("*", "*")]),
]

NODE_TYPE_NAMES = {t.name for t in NODE_TYPES}
EDGE_TYPE_NAMES = {t.name for t in EDGE_TYPES}
NODE_FIELDS: Dict[str, List[str]] = {t.name: list(t.fields) for t in NODE_TYPES}
_EDGE_BY_NAME: Dict[str, EdgeType] = {t.name: t for t in EDGE_TYPES}


def edge_endpoints_ok(label: str, source_type: str, target_type: str) -> bool:
    edge = _EDGE_BY_NAME.get(label)
    if edge is None:
        return False
    for src, dst in edge.endpoints:
        if (src in ("*", source_type)) and (dst in ("*", target_type)):
            return True
    return False


def schema_prompt_block() -> str:
    """Human/LLM-readable description of the allowed schema for the prompt."""
    lines = ["## 允许的节点类型 (type 只能取以下之一)"]
    for t in NODE_TYPES:
        fields_str = ", ".join(t.fields) if t.fields else "(无字段)"
        lines.append(f"- {t.name} ({t.cn_name}): {t.description} | 字段: {fields_str}")
    lines.append("")
    lines.append("## 允许的关系类型 (label 只能取以下之一)")
    for e in EDGE_TYPES:
        ep = ", ".join(f"{s}->{d}" for s, d in e.endpoints)
        lines.append(f"- {e.name} ({e.cn_name}): {e.description} | 端点: {ep}")
    return "\n".join(lines)


def seed_schema() -> int:
    """Insert node/edge type definitions into kg_domain_entity. Idempotent.

    Returns the number of newly inserted rows.
    """
    # The shared ``kb`` database can already be on the later V6 ontology,
    # where ``kg_domain_entity`` stores entity instances and intentionally no
    # longer has the V2 type-definition columns.  The fixed V3 ontology still
    # comes from this module, so skip persistence rather than issuing an
    # incompatible ORM query against that newer table shape.
    required_columns = {
        "entity_name",
        "cn_name",
        "entity_type",
        "core_schema",
    }
    columns = {column["name"] for column in inspect(get_engine()).get_columns("kg_domain_entity")}
    if not required_columns.issubset(columns):
        return 0

    settings = get_settings()
    inserted = 0
    with session_scope() as session:
        # V2's ``entity_name`` key is globally unique, so all graph versions
        # share this seed catalogue and must serialize on one global lock row.
        acquire_graph_write_lock(session, "__ontology__", 0)
        existing_rows = list(
            session.execute(select(DomainEntity)).scalars()
        )
        existing = {row.entity_name: row for row in existing_rows}
        for t in NODE_TYPES:
            if t.name in existing:
                existing[t.name].deleted = 0
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
        for e in EDGE_TYPES:
            if e.name in existing:
                existing[e.name].deleted = 0
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
