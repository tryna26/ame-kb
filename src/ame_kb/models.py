"""SQLAlchemy ORM models mirroring sql/001_init.sql."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    CHAR,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# SQLite only autoincrements a column whose declared type is exactly INTEGER.
# The variant keeps offline migration tests faithful without changing MySQL DDL.
_PK = BigInteger().with_variant(Integer, "sqlite")
_LONGTEXT = Text().with_variant(LONGTEXT(), "mysql")


class Base(DeclarativeBase):
    pass


class DomainEntity(Base):
    __tablename__ = "kg_domain_entity"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    entity_name: Mapped[str] = mapped_column(String(255), default="", unique=True)
    cn_name: Mapped[str] = mapped_column(String(255), default="")
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    core_schema: Mapped[Optional[str]] = mapped_column(_LONGTEXT, nullable=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GraphNode(Base):
    __tablename__ = "kg_graph_node"
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", "graph_node_no", name="uk_node_no"
        ),
        Index(
            "idx_node_merged",
            "graph_no",
            "graph_version",
            "merged_into",
            "deleted",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    graph_node_no: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(255), default="")
    type: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    aliases: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    merged_into: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    embedding_hash: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    deleted: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class GraphEdge(Base):
    __tablename__ = "kg_graph_edge"
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", "graph_edge_no", name="uk_edge_no"
        ),
        Index(
            "idx_edge_src",
            "graph_no",
            "graph_version",
            "source_node_no",
            "deleted",
        ),
        Index(
            "idx_edge_dst",
            "graph_no",
            "graph_version",
            "target_node_no",
            "deleted",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
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
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", "doc_key", name="uk_doc_key"
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    source_id: Mapped[str] = mapped_column(String(191), default="default")
    doc_id: Mapped[str] = mapped_column(String(512))
    doc_key: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NodeAlias(Base):
    __tablename__ = "kg_node_alias"
    __table_args__ = (
        UniqueConstraint(
            "graph_no",
            "graph_version",
            "type",
            "normalized_alias",
            "canonical_node_no",
            name="uk_node_alias",
        ),
        Index(
            "idx_alias_lookup",
            "graph_no",
            "graph_version",
            "type",
            "normalized_alias",
        ),
        Index(
            "idx_alias_canonical",
            "graph_no",
            "graph_version",
            "canonical_node_no",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    type: Mapped[str] = mapped_column(String(64), default="")
    normalized_alias: Mapped[str] = mapped_column(String(255))
    canonical_node_no: Mapped[str] = mapped_column(String(191))
    alias: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64), default="migration")
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class LegacyNodeId(Base):
    __tablename__ = "kg_node_legacy_id"
    __table_args__ = (
        UniqueConstraint(
            "graph_no",
            "graph_version",
            "legacy_node_no",
            name="uk_legacy_node_id",
        ),
        Index(
            "idx_legacy_canonical",
            "graph_no",
            "graph_version",
            "canonical_node_no",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    legacy_node_no: Mapped[str] = mapped_column(String(191))
    canonical_node_no: Mapped[str] = mapped_column(String(191))
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NodeContribution(Base):
    __tablename__ = "kg_node_contribution"
    __table_args__ = (
        UniqueConstraint(
            "graph_no",
            "graph_version",
            "doc_key",
            "mention_key",
            name="uk_node_contribution",
        ),
        Index(
            "idx_node_contribution_canonical",
            "graph_no",
            "graph_version",
            "canonical_node_no",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    source_id: Mapped[str] = mapped_column(String(191), default="default")
    doc_id: Mapped[str] = mapped_column(String(512))
    doc_key: Mapped[str] = mapped_column(CHAR(64))
    mention_key: Mapped[str] = mapped_column(CHAR(64))
    # Document-local extraction locator retained for audit/edge addressing.
    # It is not trusted as the graph identity because LLMs may renumber it.
    mention_id: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    canonical_node_no: Mapped[str] = mapped_column(String(191))
    # Lower ranks win property conflicts when a canonical Node is rebuilt.
    # Entity merge shifts loser contributions behind the survivor block so
    # canonical-wins remains stable across later document re-ingests.
    canonical_rank: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(255), default="")
    type: Mapped[str] = mapped_column(String(64), default="")
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    extraction_hash: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class EdgeContribution(Base):
    __tablename__ = "kg_edge_contribution"
    __table_args__ = (
        UniqueConstraint(
            "graph_no",
            "graph_version",
            "doc_key",
            "edge_key",
            name="uk_edge_contribution",
        ),
        Index(
            "idx_edge_contribution_nodes",
            "graph_no",
            "graph_version",
            "source_node_no",
            "target_node_no",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
    source_id: Mapped[str] = mapped_column(String(191), default="default")
    doc_id: Mapped[str] = mapped_column(String(512))
    doc_key: Mapped[str] = mapped_column(CHAR(64))
    edge_key: Mapped[str] = mapped_column(CHAR(64))
    source_mention_key: Mapped[str] = mapped_column(CHAR(64))
    target_mention_key: Mapped[str] = mapped_column(CHAR(64))
    source_node_no: Mapped[str] = mapped_column(String(191))
    target_node_no: Mapped[str] = mapped_column(String(191))
    name: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[str] = mapped_column(String(32), default="INFERRED")
    properties: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    extraction_hash: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MergeLog(Base):
    """Merge audit contract shared with the existing V6 schema."""

    __tablename__ = "kg_merge_log"
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", "merge_id", name="uk_merge"
        ),
        Index(
            "idx_winner",
            "graph_no",
            "graph_version",
            "winner_node_no",
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
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


class SchemaMigration(Base):
    __tablename__ = "kg_schema_migration"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    migration_key: Mapped[str] = mapped_column(String(191), unique=True)
    checksum: Mapped[Optional[str]] = mapped_column(CHAR(64), nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GraphWriteLock(Base):
    """One serialization row per graph/version for all graph mutations."""

    __tablename__ = "kg_graph_write_lock"
    __table_args__ = (
        UniqueConstraint(
            "graph_no", "graph_version", name="uk_graph_write_lock"
        ),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    graph_no: Mapped[str] = mapped_column(String(128))
    graph_version: Mapped[int] = mapped_column(BigInteger)
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
