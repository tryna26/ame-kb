"""Idempotent V3 schema bootstrap and scoped identity migration.

DDL discovery is performed against ``information_schema`` on MySQL 8.  A
small SQLite path is intentionally supported for offline migration tests.
Identity DML is always limited to the graph/version returned by Settings.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .db import acquire_graph_write_lock, get_engine
from .models import (
    Base,
    DocVersion,
    EdgeContribution,
    GraphEdge,
    GraphNode,
    GraphWriteLock,
    LegacyNodeId,
    MergeLog,
    NodeAlias,
    NodeContribution,
    SchemaMigration,
)

_IDENTITY_NAMESPACE = uuid.UUID("bdf95095-b265-4b9c-95a5-263d0398b7dc")
_REMOVE_ALIAS_CHARS = frozenset({"_", "-", "/", "\\"})


@dataclass(frozen=True)
class MigrationStats:
    nodes_migrated: int = 0
    edges_updated: int = 0
    aliases_added: int = 0
    node_contributions_added: int = 0
    edge_contributions_added: int = 0


def effective_source_id(source_id: Optional[str]) -> str:
    """Return the stable source namespace used by document keys."""

    normalized = unicodedata.normalize("NFKC", str(source_id or "")).strip()
    return normalized or "default"


def normalize_alias(value: str) -> str:
    """Normalize surface text for exact alias blocking and mention keys."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(
        ch for ch in normalized if not ch.isspace() and ch not in _REMOVE_ALIAS_CHARS
    )


def _sha256(parts: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def stable_doc_key(source_id: Optional[str], doc_id: str) -> str:
    """Return ``sha256(source_id + NUL + doc_id)`` as 64 lowercase hex."""

    return _sha256((effective_source_id(source_id), str(doc_id)))


def stable_mention_key(type_: str, name: str) -> str:
    """Return ``sha256(type + NUL + normalize(name))``."""

    return _sha256((str(type_), normalize_alias(name)))


def stable_edge_key(
    source_mention_key: str, label: str, target_mention_key: str
) -> str:
    """Return the stable, document-scoped extracted-edge key."""

    return _sha256(
        (str(source_mention_key), str(label), str(target_mention_key))
    )


def _canonical_edge_no(source_no: str, label: str, target_no: str) -> str:
    payload = f"{source_no}|{label}|{target_no}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


_V3_TABLES = (
    "kg_graph_node",
    "kg_graph_edge",
    "kg_doc_version",
    "kg_node_alias",
    "kg_node_legacy_id",
    "kg_node_contribution",
    "kg_edge_contribution",
    "kg_merge_log",
    "kg_schema_migration",
    "kg_graph_write_lock",
)
_V3_AUXILIARY_TABLES = (
    "kg_node_alias",
    "kg_node_legacy_id",
    "kg_node_contribution",
    "kg_edge_contribution",
    "kg_merge_log",
    "kg_schema_migration",
    "kg_graph_write_lock",
)
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# Columns that can be safely added to populated legacy/shared tables.  New
# tables are created from ORM metadata; these clauses also repair a partially
# provisioned V3 table without requiring procedural SQL files.
_COLUMN_DDL: Mapping[str, Mapping[str, str]] = {
    "kg_graph_node": {
        "description": "TEXT NULL",
        "aliases": "JSON NULL",
        "merged_into": "VARCHAR(191) NULL",
        "embedding": "JSON NULL",
        "embedding_hash": "CHAR(64) NULL",
        "embedding_model": "VARCHAR(255) NULL",
    },
    "kg_graph_edge": {"description": "TEXT NULL"},
    "kg_doc_version": {
        "source_id": "VARCHAR(191) NOT NULL DEFAULT 'default'",
        "doc_key": "CHAR(64) NULL",
    },
    "kg_node_alias": {
        "type": "VARCHAR(64) NOT NULL DEFAULT ''",
        "normalized_alias": "VARCHAR(255) NULL",
        "canonical_node_no": "VARCHAR(191) NULL",
        "alias": "VARCHAR(255) NULL",
        "source": "VARCHAR(64) NOT NULL DEFAULT 'migration'",
        "create_time": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
    "kg_node_legacy_id": {
        "legacy_node_no": "VARCHAR(191) NULL",
        "canonical_node_no": "VARCHAR(191) NULL",
        "create_time": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
    "kg_node_contribution": {
        "source_id": "VARCHAR(191) NOT NULL DEFAULT 'default'",
        "doc_id": "VARCHAR(512) NULL",
        "doc_key": "CHAR(64) NULL",
        "mention_key": "CHAR(64) NULL",
        "mention_id": "VARCHAR(191) NULL",
        "canonical_node_no": "VARCHAR(191) NULL",
        "canonical_rank": "INT NOT NULL DEFAULT 0",
        "name": "VARCHAR(255) NOT NULL DEFAULT ''",
        "type": "VARCHAR(64) NOT NULL DEFAULT ''",
        "properties": "JSON NULL",
        "ref": "JSON NULL",
        "extraction_hash": "CHAR(64) NULL",
        "create_time": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
    "kg_edge_contribution": {
        "source_id": "VARCHAR(191) NOT NULL DEFAULT 'default'",
        "doc_id": "VARCHAR(512) NULL",
        "doc_key": "CHAR(64) NULL",
        "edge_key": "CHAR(64) NULL",
        "source_mention_key": "CHAR(64) NULL",
        "target_mention_key": "CHAR(64) NULL",
        "source_node_no": "VARCHAR(191) NULL",
        "target_node_no": "VARCHAR(191) NULL",
        "name": "VARCHAR(64) NOT NULL DEFAULT ''",
        "confidence": "VARCHAR(32) NOT NULL DEFAULT 'INFERRED'",
        "properties": "JSON NULL",
        "ref": "JSON NULL",
        "extraction_hash": "CHAR(64) NULL",
        "create_time": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
    "kg_merge_log": {
        "merge_id": "VARCHAR(64) NULL",
        "winner_node_no": "VARCHAR(191) NULL",
        "loser_node_no": "VARCHAR(191) NULL",
        "snapshot": "JSON NULL",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'MERGED'",
        "create_time": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
    "kg_schema_migration": {
        "migration_key": "VARCHAR(191) NULL",
        "checksum": "CHAR(64) NULL",
        "applied_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
    },
    "kg_graph_write_lock": {
        "graph_no": "VARCHAR(128) NULL",
        "graph_version": "BIGINT NULL",
        "update_time": (
            "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP "
            "ON UPDATE CURRENT_TIMESTAMP"
        ),
    },
}

_INDEX_DDL: Mapping[str, Sequence[Tuple[str, bool, Sequence[str]]]] = {
    "kg_graph_node": (
        ("idx_node_merged", False, ("graph_no", "graph_version", "merged_into", "deleted")),
    ),
    "kg_graph_edge": (
        ("idx_edge_src", False, ("graph_no", "graph_version", "source_node_no", "deleted")),
        ("idx_edge_dst", False, ("graph_no", "graph_version", "target_node_no", "deleted")),
    ),
    "kg_doc_version": (
        ("uk_doc_key", True, ("graph_no", "graph_version", "doc_key")),
    ),
    "kg_node_alias": (
        ("uk_node_alias", True, ("graph_no", "graph_version", "type", "normalized_alias", "canonical_node_no")),
        ("idx_alias_lookup", False, ("graph_no", "graph_version", "type", "normalized_alias")),
        ("idx_alias_canonical", False, ("graph_no", "graph_version", "canonical_node_no")),
    ),
    "kg_node_legacy_id": (
        ("uk_legacy_node_id", True, ("graph_no", "graph_version", "legacy_node_no")),
        ("idx_legacy_canonical", False, ("graph_no", "graph_version", "canonical_node_no")),
    ),
    "kg_node_contribution": (
        ("uk_node_contribution", True, ("graph_no", "graph_version", "doc_key", "mention_key")),
        ("idx_node_contribution_canonical", False, ("graph_no", "graph_version", "canonical_node_no")),
    ),
    "kg_edge_contribution": (
        ("uk_edge_contribution", True, ("graph_no", "graph_version", "doc_key", "edge_key")),
        ("idx_edge_contribution_nodes", False, ("graph_no", "graph_version", "source_node_no", "target_node_no")),
    ),
    "kg_merge_log": (
        ("uk_merge", True, ("graph_no", "graph_version", "merge_id")),
        ("idx_winner", False, ("graph_no", "graph_version", "winner_node_no")),
    ),
    "kg_schema_migration": (
        ("uk_schema_migration", True, ("migration_key",)),
    ),
    "kg_graph_write_lock": (
        ("uk_graph_write_lock", True, ("graph_no", "graph_version")),
    ),
}


def _table_exists(conn: Connection, table_name: str) -> bool:
    if conn.dialect.name == "mysql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = :name LIMIT 1"
                ),
                {"name": table_name},
            ).scalar()
        )
    return inspect(conn).has_table(table_name)


def _column_names(conn: Connection, table_name: str) -> set:
    if conn.dialect.name == "mysql":
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :name"
            ),
            {"name": table_name},
        )
        return {str(row[0]) for row in rows}
    return {str(row["name"]) for row in inspect(conn).get_columns(table_name)}


def _index_names(conn: Connection, table_name: str) -> set:
    if conn.dialect.name == "mysql":
        rows = conn.execute(
            text(
                "SELECT DISTINCT index_name FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :name"
            ),
            {"name": table_name},
        )
        return {str(row[0]) for row in rows}
    result = {str(row["name"]) for row in inspect(conn).get_indexes(table_name)}
    for row in inspect(conn).get_unique_constraints(table_name):
        if row.get("name"):
            result.add(str(row["name"]))
    return result


def _index_columns(conn: Connection, table_name: str, index_name: str) -> Tuple[str, ...]:
    if conn.dialect.name != "mysql":
        return ()
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :table_name "
            "AND index_name = :index_name ORDER BY seq_in_index"
        ),
        {"table_name": table_name, "index_name": index_name},
    )
    return tuple(str(row[0]) for row in rows)


def _sqlite_column_ddl(ddl: str) -> str:
    # SQLite accepts JSON and VARCHAR, but not MySQL's ON UPDATE clause.
    marker = " ON UPDATE CURRENT_TIMESTAMP"
    return ddl.replace(marker, "")


def _mysql_table_collation(conn: Connection, table_name: str) -> Optional[str]:
    value = conn.execute(
        text(
            "SELECT table_collation FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :name"
        ),
        {"name": table_name},
    ).scalar_one_or_none()
    return str(value) if value else None


def _align_mysql_collations(conn: Connection) -> None:
    """Align V3 identity/reference tables with ``kg_graph_node``.

    SQLAlchemy-created tables otherwise inherit the database default, which
    can differ from the existing graph tables.  MySQL then rejects ordinary
    joins between ``graph_node_no`` and contribution/alias references with
    error 1267 (illegal mix of collations).
    """

    target = _mysql_table_collation(conn, "kg_graph_node")
    if not target or not _SQL_IDENTIFIER.fullmatch(target):
        return
    charset = target.split("_", 1)[0]
    if not _SQL_IDENTIFIER.fullmatch(charset):
        return
    for table_name in _V3_AUXILIARY_TABLES:
        if not _table_exists(conn, table_name):
            continue
        current = _mysql_table_collation(conn, table_name)
        if current == target:
            continue
        conn.exec_driver_sql(
            f"ALTER TABLE `{table_name}` CONVERT TO CHARACTER SET {charset} "
            f"COLLATE {target}"
        )


def ensure_v3_schema(engine: Optional[Engine] = None) -> None:
    """Idempotently add the V3 tables, columns, and indexes.

    Existing V6 tables (notably ``kg_merge_log``) are reused in place.  No
    table is dropped or recreated, and this function performs no identity DML.
    """

    db_engine = engine or get_engine()
    with db_engine.begin() as conn:
        for table_name in _V3_TABLES:
            table = Base.metadata.tables[table_name]
            if not _table_exists(conn, table_name):
                table.create(bind=conn, checkfirst=True)

        for table_name, definitions in _COLUMN_DDL.items():
            if not _table_exists(conn, table_name):
                continue
            existing = _column_names(conn, table_name)
            for column_name, ddl in definitions.items():
                if column_name in existing:
                    continue
                clause = _sqlite_column_ddl(ddl) if conn.dialect.name == "sqlite" else ddl
                conn.exec_driver_sql(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {clause}"
                )

        for table_name, definitions in _INDEX_DDL.items():
            if not _table_exists(conn, table_name):
                continue
            existing = _index_names(conn, table_name)
            columns = _column_names(conn, table_name)
            for index_name, unique, index_columns in definitions:
                if index_name in existing or not set(index_columns).issubset(columns):
                    continue
                quoted = ", ".join(f"`{column}`" for column in index_columns)
                unique_sql = "UNIQUE " if unique else ""
                if conn.dialect.name == "sqlite":
                    conn.exec_driver_sql(
                        f"CREATE {unique_sql}INDEX IF NOT EXISTS `{index_name}` "
                        f"ON `{table_name}` ({quoted})"
                    )
                else:
                    conn.exec_driver_sql(
                        f"ALTER TABLE `{table_name}` ADD {unique_sql}INDEX "
                        f"`{index_name}` ({quoted})"
                    )

        if conn.dialect.name == "mysql":
            _align_mysql_collations(conn)

        # V2 keyed documents only by path.  V3 keys by source+path hash, so the
        # old unique index must not reject the same doc_id in two sources.
        # Drop it before creating uk_doc_key: legacy rows all have NULL doc_key,
        # which MySQL permits in a UNIQUE index until migrate_identity backfills.
        if (
            conn.dialect.name == "mysql"
            and _table_exists(conn, "kg_doc_version")
            and _index_columns(conn, "kg_doc_version", "uk_doc")
            == ("graph_no", "graph_version", "doc_id")
        ):
            conn.exec_driver_sql(
                "ALTER TABLE `kg_doc_version` DROP INDEX `uk_doc`"
            )


def _new_node_no(graph_no: str, graph_version: int, legacy_no: str) -> str:
    seed = f"{graph_no}\0{graph_version}\0{legacy_no}"
    return f"node:{uuid.uuid5(_IDENTITY_NAMESPACE, seed)}"


def _dict(value: object) -> Dict:
    return dict(value) if isinstance(value, dict) else {}


def _merge_mapping(winner: object, loser: object) -> Dict:
    result = _dict(loser)
    result.update(_dict(winner))
    return result


def _merge_ref(winner: object, loser: object) -> Dict:
    result = _dict(winner)
    for doc_id, locations in _dict(loser).items():
        old = result.get(doc_id, [])
        old_items = old if isinstance(old, list) else [old]
        new_items = locations if isinstance(locations, list) else [locations]
        result[doc_id] = list(dict.fromkeys([*old_items, *new_items]))
    return result


def _optional_columns(conn: Connection, table_name: str) -> set:
    return _column_names(conn, table_name) if _table_exists(conn, table_name) else set()


def _replace_reference(
    session: Session,
    table_name: str,
    column_name: str,
    graph_no: str,
    graph_version: int,
    old: str,
    new: str,
    extra_predicate: str = "",
) -> None:
    conn = session.connection()
    columns = _optional_columns(conn, table_name)
    required = {"graph_no", "graph_version", column_name}
    if not required.issubset(columns):
        return
    session.execute(
        text(
            f"UPDATE `{table_name}` SET `{column_name}` = :new "
            "WHERE `graph_no` = :graph_no AND `graph_version` = :graph_version "
            f"AND `{column_name}` = :old"
            + extra_predicate
        ),
        {
            "new": new,
            "old": old,
            "graph_no": graph_no,
            "graph_version": graph_version,
        },
    )


def _legacy_docs(ref: object) -> Iterable[Tuple[str, object]]:
    if not isinstance(ref, dict):
        return ()
    return ((str(doc_id), locations) for doc_id, locations in ref.items())


def _document_hashes(
    session: Session, graph_no: str, graph_version: int
) -> Dict[str, Optional[str]]:
    rows = session.execute(
        select(DocVersion).where(
            DocVersion.graph_no == graph_no,
            DocVersion.graph_version == graph_version,
        )
    ).scalars()
    return {str(row.doc_id): row.content_hash for row in rows}


def _validate_edges(
    session: Session, graph_no: str, graph_version: int
) -> None:
    node_nos = set(
        session.execute(
            select(GraphNode.graph_node_no).where(
                GraphNode.graph_no == graph_no,
                GraphNode.graph_version == graph_version,
            )
        ).scalars()
    )
    dangling: List[str] = []
    edges = session.execute(
        select(GraphEdge).where(
            GraphEdge.graph_no == graph_no,
            GraphEdge.graph_version == graph_version,
        )
    ).scalars()
    for edge in edges:
        if edge.source_node_no not in node_nos or edge.target_node_no not in node_nos:
            dangling.append(
                f"{edge.graph_edge_no}:{edge.source_node_no}->{edge.target_node_no}"
            )
    if dangling:
        sample = ", ".join(dangling[:5])
        raise RuntimeError(
            f"identity migration found {len(dangling)} dangling edge(s): {sample}"
        )


def migrate_identity(engine: Optional[Engine] = None) -> MigrationStats:
    """Migrate only the configured graph/version to durable Node IDs.

    The legacy mapping is written before references are changed, all DML is one
    transaction, and the final dangling-edge check must pass before commit.
    Re-running a completed migration returns zero mutation counts.
    """

    db_engine = engine or get_engine()
    ensure_v3_schema(db_engine)
    settings = get_settings()
    graph_no = settings.graph_no
    graph_version = settings.graph_version
    source_id = effective_source_id(getattr(settings, "source_id", None))
    factory = sessionmaker(bind=db_engine, future=True, expire_on_commit=False)
    counts = {
        "nodes_migrated": 0,
        "edges_updated": 0,
        "aliases_added": 0,
        "node_contributions_added": 0,
        "edge_contributions_added": 0,
    }

    with factory.begin() as session:
        acquire_graph_write_lock(session, graph_no, graph_version)
        nodes = list(
            session.execute(
                select(GraphNode)
                .where(
                    GraphNode.graph_no == graph_no,
                    GraphNode.graph_version == graph_version,
                )
                .order_by(GraphNode.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        _validate_edges(session, graph_no, graph_version)
        node_by_no = {node.graph_node_no: node for node in nodes}
        old_nodes = [node for node in nodes if not node.graph_node_no.startswith("node:")]

        legacy_rows = list(
            session.execute(
                select(LegacyNodeId).where(
                    LegacyNodeId.graph_no == graph_no,
                    LegacyNodeId.graph_version == graph_version,
                )
            )
            .scalars()
            .all()
        )
        legacy_map = {row.legacy_node_no: row.canonical_node_no for row in legacy_rows}
        mappings: Dict[str, str] = {}
        for node in old_nodes:
            old_no = node.graph_node_no
            new_no = legacy_map.get(old_no) or _new_node_no(
                graph_no, graph_version, old_no
            )
            occupying = node_by_no.get(new_no)
            if occupying is not None and occupying.id != node.id:
                raise RuntimeError(
                    f"legacy mapping {old_no!r} targets occupied Node ID {new_no!r}"
                )
            mappings[old_no] = new_no
            if old_no not in legacy_map:
                session.add(
                    LegacyNodeId(
                        graph_no=graph_no,
                        graph_version=graph_version,
                        legacy_node_no=old_no,
                        canonical_node_no=new_no,
                    )
                )
        session.flush()

        edges = list(
            session.execute(
                select(GraphEdge)
                .where(
                    GraphEdge.graph_no == graph_no,
                    GraphEdge.graph_version == graph_version,
                )
                .order_by(GraphEdge.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        edge_number_map: Dict[str, str] = {}
        if mappings:
            edge_before: Dict[int, str] = {
                edge.id: edge.graph_edge_no for edge in edges
            }
            desired_groups: Dict[str, List[GraphEdge]] = {}
            for edge in edges:
                new_source = mappings.get(edge.source_node_no, edge.source_node_no)
                new_target = mappings.get(edge.target_node_no, edge.target_node_no)
                desired = _canonical_edge_no(new_source, edge.name, new_target)
                if (
                    new_source != edge.source_node_no
                    or new_target != edge.target_node_no
                    or desired != edge.graph_edge_no
                ):
                    counts["edges_updated"] += 1
                edge.source_node_no = new_source
                edge.target_node_no = new_target
                desired_groups.setdefault(desired, []).append(edge)
                edge.graph_edge_no = f"migrate:{edge.id}:{uuid.uuid4().hex}"
            session.flush()

            for desired, members in desired_groups.items():
                members.sort(key=lambda item: (int(item.deleted or 0), int(item.id)))
                survivor, duplicates = members[0], members[1:]
                survivor.graph_edge_no = desired
                edge_number_map[edge_before[survivor.id]] = desired
                for duplicate in duplicates:
                    survivor.properties = _merge_mapping(
                        survivor.properties, duplicate.properties
                    )
                    survivor.ref = _merge_ref(survivor.ref, duplicate.ref)
                    duplicate.deleted = 1
                    duplicate.graph_edge_no = f"duplicate:{duplicate.id}:{desired}"
                    edge_number_map[edge_before[duplicate.id]] = duplicate.graph_edge_no

        for node in old_nodes:
            node.graph_node_no = mappings[node.graph_node_no]
            counts["nodes_migrated"] += 1
        for node in nodes:
            if node.merged_into in mappings:
                node.merged_into = mappings[node.merged_into]
        session.flush()

        # Keep known V6/shared references aligned when those tables/columns are
        # present.  V2's differently-shaped kg_domain_entity is intentionally
        # ignored because it has no graph_node_no column.
        for old_no, new_no in mappings.items():
            for table_name, column_name in (
                ("kg_domain_entity", "graph_node_no"),
                ("kg_entity_alias", "canonical_node_no"),
                ("kg_node_alias", "canonical_node_no"),
                ("kg_node_contribution", "canonical_node_no"),
                ("kg_edge_contribution", "source_node_no"),
                ("kg_edge_contribution", "target_node_no"),
                ("kg_merge_log", "winner_node_no"),
                ("kg_merge_log", "loser_node_no"),
            ):
                _replace_reference(
                    session,
                    table_name,
                    column_name,
                    graph_no,
                    graph_version,
                    old_no,
                    new_no,
                )
            conn = session.connection()
            search_columns = _optional_columns(conn, "kg_search_index")
            if {"graph_no", "graph_version", "object_type", "object_no"}.issubset(
                search_columns
            ):
                session.execute(
                    text(
                        "UPDATE `kg_search_index` SET `object_no` = :new "
                        "WHERE `graph_no` = :graph_no AND `graph_version` = :graph_version "
                        "AND `object_type` = 'NODE' AND `object_no` = :old"
                    ),
                    {
                        "new": new_no,
                        "old": old_no,
                        "graph_no": graph_no,
                        "graph_version": graph_version,
                    },
                )

        # Edge search rows are derived data, but retaining their identifiers is
        # cheap when the shared V6 index is installed.
        for old_no, new_no in edge_number_map.items():
            _replace_reference(
                session,
                "kg_search_index",
                "object_no",
                graph_no,
                graph_version,
                old_no,
                new_no,
                " AND `object_type` = 'EDGE'",
            )

        alias_keys = set(
            session.execute(
                select(
                    NodeAlias.type,
                    NodeAlias.normalized_alias,
                    NodeAlias.canonical_node_no,
                ).where(
                    NodeAlias.graph_no == graph_no,
                    NodeAlias.graph_version == graph_version,
                )
            ).all()
        )
        for node in nodes:
            surfaces = list(node.aliases) if isinstance(node.aliases, list) else []
            surfaces = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in [node.name, *surfaces]
                    if str(value or "").strip()
                )
            )
            node.aliases = surfaces
            seen_normalized = set()
            for alias in surfaces:
                normalized = normalize_alias(alias)
                key = (node.type, normalized, node.graph_node_no)
                if not normalized or normalized in seen_normalized or key in alias_keys:
                    continue
                seen_normalized.add(normalized)
                alias_keys.add(key)
                session.add(
                    NodeAlias(
                        graph_no=graph_no,
                        graph_version=graph_version,
                        type=node.type,
                        normalized_alias=normalized,
                        canonical_node_no=node.graph_node_no,
                        alias=alias,
                        source="migration",
                    )
                )
                counts["aliases_added"] += 1

        doc_hashes = _document_hashes(session, graph_no, graph_version)
        node_contribution_keys = set(
            session.execute(
                select(NodeContribution.doc_key, NodeContribution.mention_key).where(
                    NodeContribution.graph_no == graph_no,
                    NodeContribution.graph_version == graph_version,
                )
            ).all()
        )
        for node in nodes:
            mention_key = stable_mention_key(node.type, node.name)
            legacy_docs = list(_legacy_docs(node.ref))
            # A V1/V2 aggregate does not record which document supplied which
            # property.  Copying the aggregate payload into every contribution
            # would make a removed fact immortal through the other documents.
            # Preserve payload only when attribution is unambiguous; otherwise
            # keep structural provenance and let real re-ingest rebuild facts.
            attributable_properties = (
                _dict(node.properties) if len(legacy_docs) == 1 else {}
            )
            for doc_id, locations in legacy_docs:
                doc_key = stable_doc_key(source_id, doc_id)
                key = (doc_key, mention_key)
                if key in node_contribution_keys:
                    continue
                node_contribution_keys.add(key)
                session.add(
                    NodeContribution(
                        graph_no=graph_no,
                        graph_version=graph_version,
                        source_id=source_id,
                        doc_id=doc_id,
                        doc_key=doc_key,
                        mention_key=mention_key,
                        mention_id=None,
                        canonical_node_no=node.graph_node_no,
                        canonical_rank=0,
                        name=node.name,
                        type=node.type,
                        properties=attributable_properties,
                        ref={f"{source_id}::{doc_id}": locations},
                        extraction_hash=doc_hashes.get(doc_id),
                    )
                )
                counts["node_contributions_added"] += 1

        current_nodes = {node.graph_node_no: node for node in nodes}
        edge_contribution_keys = set(
            session.execute(
                select(EdgeContribution.doc_key, EdgeContribution.edge_key).where(
                    EdgeContribution.graph_no == graph_no,
                    EdgeContribution.graph_version == graph_version,
                )
            ).all()
        )
        for edge in edges:
            if int(edge.deleted or 0) != 0:
                continue
            source = current_nodes[edge.source_node_no]
            target = current_nodes[edge.target_node_no]
            source_mention = stable_mention_key(source.type, source.name)
            target_mention = stable_mention_key(target.type, target.name)
            contribution_key = stable_edge_key(
                source_mention, edge.name, target_mention
            )
            legacy_docs = list(_legacy_docs(edge.ref))
            attributable_props = _dict(edge.properties) if len(legacy_docs) == 1 else {}
            confidence = (
                str(attributable_props.get("confidence") or "INFERRED")
                if len(legacy_docs) == 1
                else "AMBIGUOUS"
            )
            for doc_id, locations in legacy_docs:
                doc_key = stable_doc_key(source_id, doc_id)
                key = (doc_key, contribution_key)
                if key in edge_contribution_keys:
                    continue
                edge_contribution_keys.add(key)
                session.add(
                    EdgeContribution(
                        graph_no=graph_no,
                        graph_version=graph_version,
                        source_id=source_id,
                        doc_id=doc_id,
                        doc_key=doc_key,
                        edge_key=contribution_key,
                        source_mention_key=source_mention,
                        target_mention_key=target_mention,
                        source_node_no=edge.source_node_no,
                        target_node_no=edge.target_node_no,
                        name=edge.name,
                        confidence=confidence,
                        properties=attributable_props,
                        ref={f"{source_id}::{doc_id}": locations},
                        extraction_hash=doc_hashes.get(doc_id),
                    )
                )
                counts["edge_contributions_added"] += 1

        # Populate the new DocVersion identity columns without changing the old
        # (graph, version, doc_id) compatibility key.
        for doc in session.execute(
            select(DocVersion).where(
                DocVersion.graph_no == graph_no,
                DocVersion.graph_version == graph_version,
            )
        ).scalars():
            if not doc.doc_key:
                # A legacy row acquired the column's database default during
                # ALTER TABLE.  The configured source is the authoritative
                # namespace for this scoped one-time migration.
                doc.source_id = source_id
                doc.doc_key = stable_doc_key(doc.source_id, doc.doc_id)
            else:
                doc.source_id = effective_source_id(doc.source_id)

        _validate_edges(session, graph_no, graph_version)

    return MigrationStats(**counts)
