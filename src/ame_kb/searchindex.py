"""Build and upsert kg_search_index rows for hybrid recall.

searchable_text = name + description + flattened properties, mirroring
general_recall/core/entity/byterag_store.go graphNodeToMap (rag_content =
name + properties) and extractRAGTextFromProperties (properties JSON flattened
to "key: value", non-string values skipped). Each row also carries an embedding
(JSON) when the embedding endpoint is configured; otherwise embedding is left
NULL and recall degrades to FULLTEXT-only.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .searchbackend import IndexEntry, get_index

NODE = "NODE"
EDGE = "EDGE"
DOC_CHUNK = "DOC_CHUNK"


def build_searchable_text(
    name: str,
    description: Optional[str],
    properties: Optional[Dict],
    aliases: Optional[Sequence[str]] = None,
) -> str:
    """Concatenate name, description, flattened string properties, and aliases.

    Empty/None parts are skipped; non-string property values are skipped
    (extractRAGTextFromProperties behaviour). Aliases (V5) are appended so a
    merged-away name resolves to its canonical entity via full-text/vector.
    """
    parts: List[str] = []
    if name and name.strip():
        parts.append(name.strip())
    if description and description.strip():
        parts.append(description.strip())
    for key, value in (properties or {}).items():
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
    for alias in aliases or []:
        if alias and alias.strip():
            parts.append(alias.strip())
    return "\n".join(parts)


def aliases_for(session: Session, canonical_node_nos: Sequence[str]) -> Dict[str, List[str]]:
    """Return {canonical_node_no: [alias, ...]} for the given nodes (V5)."""
    from .config import get_settings
    from .models import EntityAlias

    if not canonical_node_nos:
        return {}
    settings = get_settings()
    rows = (
        session.execute(
            select(EntityAlias.canonical_node_no, EntityAlias.alias).where(
                EntityAlias.graph_no == settings.graph_no,
                EntityAlias.graph_version == settings.graph_version,
                EntityAlias.canonical_node_no.in_(list(canonical_node_nos)),
            )
        )
        .all()
    )
    out: Dict[str, List[str]] = {}
    for canonical_no, alias in rows:
        out.setdefault(canonical_no, []).append(alias)
    return out


def upsert_search_index(session: Session, entries: List[Dict]) -> int:
    """Upsert search-index rows for a batch of objects through the HybridIndex.

    entries: [{"object_type": NODE|EDGE|DOC_CHUNK, "object_no": str,
               "searchable_text": str}]
    Embeddings are computed by the backend in one batch call when available.
    Returns the number of rows written. The caller's `session` is passed through
    so a SQL backend joins the same transaction.
    """
    if not entries:
        return 0
    index_entries = [
        IndexEntry(
            object_type=e["object_type"],
            object_no=e["object_no"],
            searchable_text=e["searchable_text"],
        )
        for e in entries
    ]
    return get_index().upsert(index_entries, session=session)
