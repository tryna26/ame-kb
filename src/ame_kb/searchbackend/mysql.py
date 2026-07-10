"""MySQL HybridIndex: FULLTEXT(ngram) text channel + JSON-embedding vector
channel, fused client-side with RRF.

This is the V3 recall._fulltext_channel / _vector_channel / _hybrid_recall code
moved behind the HybridIndex port with behavior preserved: same SQL, same
in-memory cosine, same RRF fusion. It is the default backend and the reference
implementation the Redis backend must match for SEARCH_BACKEND=redis|mysql
result parity.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..embed import embed_texts, embedding_available
from ..models import SearchIndex
from ..rrf import rrf_merge
from ..vecmath import cosine
from .base import HybridIndex, IndexEntry, SearchFilters


class MysqlHybridIndex(HybridIndex):
    def _fulltext_channel(
        self,
        session: Session,
        query: str,
        filters: SearchFilters,
        min_score: float,
        limit: int,
    ) -> List[str]:
        sql = (
            "SELECT object_no, "
            "MATCH(searchable_text) AGAINST(:q IN NATURAL LANGUAGE MODE) AS score "
            "FROM kg_search_index "
            "WHERE object_type = :t "
            "AND MATCH(searchable_text) AGAINST(:q IN NATURAL LANGUAGE MODE) > :min "
        )
        params = {
            "q": query,
            "t": filters.object_type,
            "min": min_score,
            "lim": limit,
        }
        if not filters.ignore_graph_scope:
            sql += "AND graph_no = :g AND graph_version = :v "
            params["g"] = filters.graph_no
            params["v"] = filters.graph_version
        expanding = []
        if filters.restrict_object_nos is not None:
            sql += "AND object_no IN :nos "
            params["nos"] = list(filters.restrict_object_nos)
            expanding.append(bindparam("nos", expanding=True))
        sql += "ORDER BY score DESC LIMIT :lim"
        stmt = text(sql)
        if expanding:
            stmt = stmt.bindparams(*expanding)
        rows = session.execute(stmt, params).all()
        return [r[0] for r in rows]

    def _vector_channel(
        self,
        session: Session,
        query_embedding: Sequence[float],
        filters: SearchFilters,
        min_score: float,
        limit: int,
    ) -> List[str]:
        conds = [
            SearchIndex.object_type == filters.object_type,
            SearchIndex.embedding.is_not(None),
        ]
        if not filters.ignore_graph_scope:
            conds.append(SearchIndex.graph_no == filters.graph_no)
            conds.append(SearchIndex.graph_version == filters.graph_version)
        if filters.restrict_object_nos is not None:
            conds.append(
                SearchIndex.object_no.in_(list(filters.restrict_object_nos))
            )
        rows = session.execute(
            select(SearchIndex.object_no, SearchIndex.embedding).where(*conds)
        ).all()
        scored: List[Tuple[str, float]] = []
        for object_no, emb in rows:
            if not emb:
                continue
            score = cosine(query_embedding, emb)
            if score > min_score:
                scored.append((object_no, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [no for no, _ in scored[:limit]]

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters,
        limit: int,
        min_score_text: float,
        min_score_embedding: float,
        query_embedding: Optional[Sequence[float]] = None,
        warnings: Optional[List[str]] = None,
    ) -> List[str]:
        warnings = warnings if warnings is not None else []
        channels: List[List[str]] = []
        with session_scope() as session:
            try:
                channels.append(
                    self._fulltext_channel(
                        session, query, filters, min_score_text, limit
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one channel is non-fatal
                warnings.append(
                    f"fulltext {filters.object_type} channel failed: {exc}"
                )
            if query_embedding is not None:
                try:
                    channels.append(
                        self._vector_channel(
                            session,
                            query_embedding,
                            filters,
                            min_score_embedding,
                            limit,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"vector {filters.object_type} channel failed: {exc}"
                    )
        if not channels:
            return []
        return rrf_merge(channels)[:limit]

    def upsert(self, entries: Sequence[IndexEntry], *, session=None) -> int:
        if not entries:
            return 0
        if session is None:
            with session_scope() as own:
                return self._upsert(list(entries), own)
        return self._upsert(list(entries), session)

    def _upsert(self, entries: List[IndexEntry], session: Session) -> int:
        settings = get_settings()
        # Compute any missing embeddings in one batch call.
        missing = [e for e in entries if e.embedding is None]
        if missing and embedding_available():
            vectors = embed_texts([e.searchable_text for e in missing])
            for entry, vec in zip(missing, vectors):
                entry.embedding = vec

        written = 0
        for entry in entries:
            existing = session.execute(
                select(SearchIndex).where(
                    SearchIndex.graph_no == settings.graph_no,
                    SearchIndex.graph_version == settings.graph_version,
                    SearchIndex.object_type == entry.object_type,
                    SearchIndex.object_no == entry.object_no,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    SearchIndex(
                        graph_no=settings.graph_no,
                        graph_version=settings.graph_version,
                        object_type=entry.object_type,
                        object_no=entry.object_no,
                        searchable_text=entry.searchable_text,
                        embedding=entry.embedding,
                    )
                )
            else:
                existing.searchable_text = entry.searchable_text
                existing.embedding = entry.embedding
            written += 1
        return written

    def delete_objects(
        self, object_type: str, object_nos: Sequence[str], *, session=None
    ) -> int:
        if not object_nos:
            return 0
        if session is None:
            with session_scope() as own:
                return self._delete_objects(object_type, list(object_nos), own)
        return self._delete_objects(object_type, list(object_nos), session)

    def _delete_objects(
        self, object_type: str, object_nos: List[str], session: Session
    ) -> int:
        settings = get_settings()
        from sqlalchemy import delete as sa_delete

        res = session.execute(
            sa_delete(SearchIndex).where(
                SearchIndex.graph_no == settings.graph_no,
                SearchIndex.graph_version == settings.graph_version,
                SearchIndex.object_type == object_type,
                SearchIndex.object_no.in_(object_nos),
            )
        )
        return res.rowcount or 0

    def delete_by_filter(self, filters: SearchFilters, *, session=None) -> int:
        if session is None:
            with session_scope() as own:
                return self._delete_by_filter(filters, own)
        return self._delete_by_filter(filters, session)

    def _delete_by_filter(self, filters: SearchFilters, session: Session) -> int:
        from sqlalchemy import delete as sa_delete

        conds = [SearchIndex.object_type == filters.object_type]
        if not filters.ignore_graph_scope:
            conds.append(SearchIndex.graph_no == filters.graph_no)
            conds.append(SearchIndex.graph_version == filters.graph_version)
        if filters.restrict_object_nos is not None:
            conds.append(
                SearchIndex.object_no.in_(list(filters.restrict_object_nos))
            )
        res = session.execute(sa_delete(SearchIndex).where(*conds))
        return res.rowcount or 0

    def copy_version(
        self,
        graph_no: str,
        object_type: str,
        object_nos: Sequence[str],
        from_version: int,
        to_version: int,
        *,
        session=None,
    ) -> int:
        object_nos = list(object_nos)
        if not object_nos:
            return 0
        if session is None:
            with session_scope() as own:
                return self._copy_version(
                    graph_no, object_type, object_nos, from_version, to_version, own
                )
        return self._copy_version(
            graph_no, object_type, object_nos, from_version, to_version, session
        )

    def _copy_version(
        self,
        graph_no: str,
        object_type: str,
        object_nos: List[str],
        from_version: int,
        to_version: int,
        session: Session,
    ) -> int:
        rows = (
            session.execute(
                select(SearchIndex).where(
                    SearchIndex.graph_no == graph_no,
                    SearchIndex.graph_version == from_version,
                    SearchIndex.object_type == object_type,
                    SearchIndex.object_no.in_(object_nos),
                )
            )
            .scalars()
            .all()
        )
        for r in rows:
            session.add(
                SearchIndex(
                    graph_no=graph_no,
                    graph_version=to_version,
                    object_type=r.object_type,
                    object_no=r.object_no,
                    searchable_text=r.searchable_text,
                    embedding=r.embedding,
                    workspace_id=r.workspace_id,
                )
            )
        return len(rows)
