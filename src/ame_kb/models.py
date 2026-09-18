"""SQLAlchemy ORM models mirroring sql/001_init.sql."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Graph(Base):
    __tablename__ = "kg_graph"
    __table_args__ = (
        UniqueConstraint("graph_no", "graph_version", name="uk_graph_ver"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128))
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    projection_done: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GraphFile(Base):
    __tablename__ = "kg_graph_file"
    __table_args__ = (
        UniqueConstraint("graph_no", "doc_no", name="uk_graph_file"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128))
    doc_no: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="")
    origin_url: Mapped[str] = mapped_column(String(1024), default="")
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Task(Base):
    __tablename__ = "kg_task"
    __table_args__ = (
        UniqueConstraint("task_no", name="uk_task_no"),
        UniqueConstraint("active_key", name="uk_active_graph_task"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(32), default="INGEST")
    graph_no: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class PipelineRun(Base):
    __tablename__ = "kg_pipeline_run"
    __table_args__ = (
        UniqueConstraint("run_no", name="uk_run_no"),
        UniqueConstraint("task_no", name="uk_run_task"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(64))
    task_no: Mapped[str] = mapped_column(String(64))
    graph_no: Mapped[str] = mapped_column(String(128))
    base_version: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    target_version: Mapped[int] = mapped_column(BigInteger, default=1)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    completed_steps: Mapped[int] = mapped_column(Integer, default=0)
    failed_steps: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class PipelineStep(Base):
    __tablename__ = "kg_pipeline_step"
    __table_args__ = (
        UniqueConstraint("step_no", name="uk_step_no"),
        UniqueConstraint("run_no", "step_key", name="uk_run_step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    step_no: Mapped[str] = mapped_column(String(64))
    run_no: Mapped[str] = mapped_column(String(64))
    step_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DomainEntity(Base):
    """Domain instance layer: one row per graph node, joined by graph_node_no.

    Holds the ontology class (type = Asset/Relation/Event/Behavior) and the Asset
    archetype (entity_spec), which used to live on kg_graph_node.
    """

    __tablename__ = "kg_domain_entity"
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", "graph_node_no", name="uk_domain_node"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    graph_node_no: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(255), default="")
    # Ontology class: Asset / Relation / Event / Behavior.
    type: Mapped[str] = mapped_column(String(64), default="")
    # Asset archetype, only set when type == "Asset":
    # Mission / Solution / Implementation / ServiceInstance / Artifact.
    entity_spec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GraphNode(Base):
    __tablename__ = "kg_graph_node"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    graph_node_no: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(255), default="")
    # Structural role: NODE / ENTITY / SKILL (extraction always emits ENTITY).
    # The ontology class + archetype live in kg_domain_entity (joined by
    # graph_node_no).
    type: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GraphEdge(Base):
    __tablename__ = "kg_graph_edge"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    graph_edge_no: Mapped[str] = mapped_column(String(191))
    source_node_no: Mapped[str] = mapped_column(String(191))
    target_node_no: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DocVersion(Base):
    __tablename__ = "kg_doc_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    doc_id: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64))
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Doc(Base):
    __tablename__ = "kg_doc"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    doc_no: Mapped[str] = mapped_column(String(512))
    path: Mapped[str] = mapped_column(String(1024), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32), default="")
    origin_url: Mapped[str] = mapped_column(String(1024), default="")
    workspace_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DocChunk(Base):
    __tablename__ = "kg_doc_chunk"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    doc_no: Mapped[str] = mapped_column(String(512))
    chunk_no: Mapped[str] = mapped_column(String(191))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origin_url: Mapped[str] = mapped_column(String(1024), default="")
    file_path: Mapped[str] = mapped_column(String(1024), default="")
    line_start: Mapped[int] = mapped_column(Integer, default=0)
    line_end: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    workspace_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DocLine(Base):
    __tablename__ = "kg_doc_line"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    doc_no: Mapped[str] = mapped_column(String(512))
    line_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EntityAlias(Base):
    __tablename__ = "kg_entity_alias"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    canonical_node_no: Mapped[str] = mapped_column(String(191))
    alias: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64), default="merge")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MergeLog(Base):
    __tablename__ = "kg_merge_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    merge_id: Mapped[str] = mapped_column(String(64))
    winner_node_no: Mapped[str] = mapped_column(String(191))
    loser_node_no: Mapped[str] = mapped_column(String(191))
    snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="MERGED")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SearchIndex(Base):
    __tablename__ = "kg_search_index"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    object_type: Mapped[str] = mapped_column(String(16))
    object_no: Mapped[str] = mapped_column(String(191))
    searchable_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
