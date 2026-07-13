"""Runtime configuration loaded from environment (.env).

Graph selection is request/task-local. The environment supplies the default
graph, while :func:`graph_context` overlays ``graph_no`` / ``graph_version``
through ``contextvars``. This keeps existing ``get_settings()`` call sites
working without letting concurrent requests mutate process-wide graph state.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator, Optional

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    embed_api_key: str
    embed_base_url: str
    embed_model: str
    mysql_dsn: str
    source_dir: str
    graph_no: str
    graph_version: int
    recall_topk: int
    recall_neighbor_topk: int
    min_score_text: float
    min_score_embedding: float
    # V4: search backend + doc-chunk + multi-hop + retry-ladder knobs.
    search_backend: str
    redis_url: str
    redis_index_prefix: str
    embed_dim: int
    chunk_size: int
    chunk_overlap: int
    recall_max_queries: int
    recall_max_hops: int
    doc_chunk_topk: int
    recall_min_results: int
    retry_strict_text: float
    retry_strict_embedding: float
    # V5: entity fusion (resolve) knobs.
    resolve_candidate_topk: int
    resolve_min_score_embedding: float
    # V6.2: durable pipeline + optional Redis wake-up queue.
    pipeline_queue_backend: str
    pipeline_redis_url: str
    pipeline_queue_key: str
    pipeline_poll_seconds: float
    pipeline_retry_delay_seconds: int
    pipeline_lease_seconds: int


@dataclass(frozen=True)
class GraphContext:
    """Request/task-local graph selection."""

    graph_no: str
    graph_version: int


_graph_context: ContextVar[Optional[GraphContext]] = ContextVar(
    "ame_kb_graph_context", default=None
)


def set_graph_context(graph_no: str, graph_version: int) -> Token:
    """Select a graph for the current context and return a reset token."""

    return _graph_context.set(GraphContext(graph_no, graph_version))


def reset_graph_context(token: Token) -> None:
    """Restore the graph context that preceded ``set_graph_context``."""

    _graph_context.reset(token)


def clear_graph_context() -> None:
    """Return the current context to the environment-configured default."""

    _graph_context.set(None)


@contextmanager
def graph_context(graph_no: str, graph_version: int) -> Iterator[GraphContext]:
    """Temporarily select a graph, isolated from concurrent async tasks."""

    selected = GraphContext(graph_no, graph_version)
    token = _graph_context.set(selected)
    try:
        yield selected
    finally:
        _graph_context.reset(token)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return val


@lru_cache(maxsize=1)
def _base_settings() -> Settings:
    return Settings(
        llm_api_key=_require("LLM_API_KEY"),
        llm_base_url=_require("LLM_BASE_URL"),
        llm_model=_require("LLM_MODEL"),
        embed_api_key=os.getenv("EMBED_API_KEY", ""),
        embed_base_url=os.getenv("EMBED_BASE_URL", ""),
        embed_model=os.getenv("EMBED_MODEL", ""),
        mysql_dsn=_require("MYSQL_DSN"),
        source_dir=os.getenv("SOURCE_DIR", "./data"),
        graph_no=os.getenv("GRAPH_NO", "default"),
        graph_version=int(os.getenv("GRAPH_VERSION", "1")),
        recall_topk=int(os.getenv("RECALL_TOPK", "5")),
        recall_neighbor_topk=int(os.getenv("RECALL_NEIGHBOR_TOPK", "3")),
        min_score_text=float(os.getenv("MIN_SCORE_TEXT", "0")),
        min_score_embedding=float(os.getenv("MIN_SCORE_EMBEDDING", "0")),
        search_backend=os.getenv("SEARCH_BACKEND", "mysql"),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_index_prefix=os.getenv("REDIS_INDEX_PREFIX", "amekb"),
        embed_dim=int(os.getenv("EMBED_DIM", "0")),
        chunk_size=int(os.getenv("CHUNK_SIZE", "800")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "200")),
        recall_max_queries=int(os.getenv("RECALL_MAX_QUERIES", "1")),
        recall_max_hops=int(os.getenv("RECALL_MAX_HOPS", "1")),
        doc_chunk_topk=int(os.getenv("DOC_CHUNK_TOPK", "10")),
        recall_min_results=int(os.getenv("RECALL_MIN_RESULTS", "0")),
        retry_strict_text=float(os.getenv("RETRY_STRICT_TEXT", "0.8")),
        retry_strict_embedding=float(os.getenv("RETRY_STRICT_EMBEDDING", "0.8")),
        resolve_candidate_topk=int(os.getenv("RESOLVE_CANDIDATE_TOPK", "10")),
        resolve_min_score_embedding=float(
            os.getenv("RESOLVE_MIN_SCORE_EMBEDDING", "0")
        ),
        pipeline_queue_backend=os.getenv("PIPELINE_QUEUE_BACKEND", "database"),
        pipeline_redis_url=os.getenv(
            "PIPELINE_REDIS_URL", "redis://localhost:6379/1"
        ),
        pipeline_queue_key=os.getenv("PIPELINE_QUEUE_KEY", "amekb:pipeline:ready"),
        pipeline_poll_seconds=float(os.getenv("PIPELINE_POLL_SECONDS", "2")),
        pipeline_retry_delay_seconds=int(
            os.getenv("PIPELINE_RETRY_DELAY_SECONDS", "5")
        ),
        pipeline_lease_seconds=int(os.getenv("PIPELINE_LEASE_SECONDS", "900")),
    )


def get_settings() -> Settings:
    """Return base settings overlaid with the current graph context, if any."""

    settings = _base_settings()
    selected = _graph_context.get()
    if selected is None:
        return settings
    return settings.model_copy(
        update={
            "graph_no": selected.graph_no,
            "graph_version": selected.graph_version,
        }
    )


def get_default_settings() -> Settings:
    """Return environment-backed settings without a graph-context overlay."""

    return _base_settings()


# Preserve the cache hook used by existing tests/callers that update env-based
# settings. Graph-context changes themselves do not need a cache clear.
get_settings.cache_clear = _base_settings.cache_clear  # type: ignore[attr-defined]
