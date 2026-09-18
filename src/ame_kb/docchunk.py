"""Persist document chunks and index them as the doc-chunk retrieval channel.

One doc maps to many chunk rows, so there is no single chunk id to key a skip on
(byterag_store.go:204 DocChunkUpToDate). Instead every chunk row stores the
parent doc's sha256; if the current doc hash already matches the stored chunks we
skip re-chunking, otherwise we delete the doc's old chunks by filter and rebuild
(byterag_store.go:522 delete-by-filter). Chunks are indexed into kg_search_index
with object_type=DOC_CHUNK so recall can add a third fallback channel.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import delete, select

from .chunker import split_text
from .config import get_settings
from .db import session_scope
from .models import DocChunk
from .searchbackend import IndexEntry, get_index
from .searchindex import DOC_CHUNK

_CHUNK_BATCH = 32


def chunk_no(doc_no: str, chunk_index: int) -> str:
    key = f"{doc_no}#{chunk_index}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def chunks_up_to_date(session, doc_no: str, sha256: str) -> bool:
    """True if this doc already has chunks stored at the given content hash."""
    if not sha256:
        return False
    settings = get_settings()
    row = session.execute(
        select(DocChunk.sha256)
        .where(
            DocChunk.graph_no == settings.graph_no,
            DocChunk.graph_version == settings.graph_version,
            DocChunk.doc_no == doc_no,
        )
        .limit(1)
    ).scalar_one_or_none()
    return row is not None and row == sha256


def _delete_doc_chunks(session, doc_no: str) -> None:
    """Drop a doc's chunk rows and their search-index rows (delete-by-filter)."""
    settings = get_settings()
    old = (
        session.execute(
            select(DocChunk.chunk_no).where(
                DocChunk.graph_no == settings.graph_no,
                DocChunk.graph_version == settings.graph_version,
                DocChunk.doc_no == doc_no,
            )
        )
        .scalars()
        .all()
    )
    if old:
        get_index().delete_objects(DOC_CHUNK, list(old), session=session)
    session.execute(
        delete(DocChunk).where(
            DocChunk.graph_no == settings.graph_no,
            DocChunk.graph_version == settings.graph_version,
            DocChunk.doc_no == doc_no,
        )
    )


def persist_chunks(
    doc_no: str,
    text: str,
    sha256: str,
    *,
    origin_url: str = "",
    file_path: str = "",
    force: bool = False,
) -> int:
    """Chunk `text`, store rows + search index. Skips when the doc hash is
    unchanged (unless force). Returns the number of chunks written."""
    settings = get_settings()
    with session_scope() as session:
        if not force and chunks_up_to_date(session, doc_no, sha256):
            return 0
        _delete_doc_chunks(session, doc_no)

        chunks = split_text(text, settings.chunk_size, settings.chunk_overlap)
        entries = []
        for ch in chunks:
            cno = chunk_no(doc_no, ch.chunk_index)
            session.add(
                DocChunk(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    doc_no=doc_no,
                    chunk_no=cno,
                    chunk_index=ch.chunk_index,
                    content=ch.content,
                    origin_url=origin_url,
                    file_path=file_path,
                    line_start=ch.line_start,
                    line_end=ch.line_end,
                    sha256=sha256,
                )
            )
            entries.append(
                IndexEntry(
                    object_type=DOC_CHUNK,
                    object_no=cno,
                    searchable_text=ch.content,
                )
            )
        for start in range(0, len(entries), _CHUNK_BATCH):
            get_index().upsert(entries[start : start + _CHUNK_BATCH], session=session)
        return len(chunks)
