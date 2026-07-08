"""Build and upsert kg_search_index rows for hybrid recall.

searchable_text = name + description + flattened properties, mirroring
general_recall/core/entity/byterag_store.go graphNodeToMap (rag_content =
name + properties) and extractRAGTextFromProperties (properties JSON flattened
to "key: value", non-string values skipped). Each row also carries an embedding
(JSON) when the embedding endpoint is configured; otherwise embedding is left
NULL and recall degrades to FULLTEXT-only.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .embed import embed_texts, embedding_available
from .models import SearchIndex

NODE = "NODE"
EDGE = "EDGE"


def build_searchable_text(
    name: str, description: Optional[str], properties: Optional[Dict]
) -> str:
    """Concatenate name, description, and flattened string properties.

    Empty/None parts are skipped; non-string property values are skipped
    (extractRAGTextFromProperties behaviour).
    """
    parts: List[str] = []
    if name and name.strip():
        parts.append(name.strip())
    if description and description.strip():
        parts.append(description.strip())
    for key, value in (properties or {}).items():
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
    return "\n".join(parts)


def upsert_search_index(session: Session, entries: List[Dict]) -> int:
    """Upsert search-index rows for a batch of objects.

    entries: [{"object_type": NODE|EDGE, "object_no": str, "searchable_text": str}]
    Embeddings are computed in one batch call when the endpoint is available.
    Returns the number of rows written (inserted + updated).
    """
    if not entries:
        return 0
    settings = get_settings()
    texts = [e["searchable_text"] for e in entries]
    embeddings: List[Optional[List[float]]] = [None] * len(entries)
    if embedding_available():
        embeddings = embed_texts(texts)  # type: ignore[assignment]

    written = 0
    for entry, emb in zip(entries, embeddings):
        existing = session.execute(
            select(SearchIndex).where(
                SearchIndex.graph_no == settings.graph_no,
                SearchIndex.graph_version == settings.graph_version,
                SearchIndex.object_type == entry["object_type"],
                SearchIndex.object_no == entry["object_no"],
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                SearchIndex(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    object_type=entry["object_type"],
                    object_no=entry["object_no"],
                    searchable_text=entry["searchable_text"],
                    embedding=emb,
                )
            )
        else:
            existing.searchable_text = entry["searchable_text"]
            existing.embedding = emb
        written += 1
    return written
