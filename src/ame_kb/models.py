"""SQLAlchemy ORM models mirroring sql/001_init.sql."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.mysql import JSON, LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DomainEntity(Base):
    __tablename__ = "kg_domain_entity"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_name: Mapped[str] = mapped_column(String(255), default="", unique=True)
    cn_name: Mapped[str] = mapped_column(String(255), default="")
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    core_schema: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True)
    graph_no: Mapped[str] = mapped_column(String(128), default="default")
    graph_version: Mapped[int] = mapped_column(BigInteger, default=1)
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
    type: Mapped[str] = mapped_column(String(64), default="")
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
