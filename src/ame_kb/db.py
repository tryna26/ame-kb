"""Database engine/session helpers and a connectivity probe."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.mysql_dsn, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def acquire_graph_write_lock(
    session: Session, graph_no: str, graph_version: int
) -> None:
    """Serialize all mutations for one logical graph/version.

    The stable lock row removes the "no aggregate row exists yet" race and
    gives ingest, merge, rollback, and identity migration one lock ordering.
    MySQL holds the row lock until the caller's transaction commits.
    """

    params = {"graph_no": graph_no, "graph_version": graph_version}
    dialect = session.get_bind().dialect.name
    if dialect == "mysql":
        session.execute(
            text(
                "INSERT INTO kg_graph_write_lock (graph_no, graph_version) "
                "VALUES (:graph_no, :graph_version) "
                "ON DUPLICATE KEY UPDATE graph_no = VALUES(graph_no)"
            ),
            params,
        )
    elif dialect == "sqlite":
        session.execute(
            text(
                "INSERT OR IGNORE INTO kg_graph_write_lock "
                "(graph_no, graph_version) VALUES (:graph_no, :graph_version)"
            ),
            params,
        )
    else:
        session.execute(
            text(
                "INSERT INTO kg_graph_write_lock (graph_no, graph_version) "
                "VALUES (:graph_no, :graph_version) "
                "ON CONFLICT (graph_no, graph_version) DO NOTHING"
            ),
            params,
        )
    session.execute(
        text(
            "SELECT id FROM kg_graph_write_lock "
            "WHERE graph_no = :graph_no AND graph_version = :graph_version "
            "FOR UPDATE"
            if dialect != "sqlite"
            else "SELECT id FROM kg_graph_write_lock "
            "WHERE graph_no = :graph_no AND graph_version = :graph_version"
        ),
        params,
    ).scalar_one()


def ping() -> bool:
    """Return True if the database answers SELECT 1, else raise with a hint."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - surface a friendly hint
        raise RuntimeError(
            "Cannot reach MySQL. Check MYSQL_DSN, and that your IP is on the "
            "Aliyun RDS whitelist / the public endpoint is enabled.\n"
            f"Underlying error: {exc}"
        ) from exc
