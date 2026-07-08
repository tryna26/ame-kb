"""Embedding client for the (separate) OpenAI-compatible /embeddings endpoint.

Shared by indexing (searchindex.py) and query time (recall.py). Mirrors the
OpenAI client style in extract.py, but points at EMBED_* config so the embedding
service can be a different endpoint than the chat LLM.
"""
from __future__ import annotations

from typing import List

from openai import OpenAI

from .config import get_settings

# Some providers (e.g. Aliyun text-embedding-v4) cap inputs per request.
_MAX_BATCH = 10


def embedding_available() -> bool:
    s = get_settings()
    return bool(s.embed_api_key and s.embed_base_url and s.embed_model)


def _client() -> OpenAI:
    settings = get_settings()
    if not embedding_available():
        raise RuntimeError(
            "Embedding endpoint is not configured. Fill EMBED_API_KEY / "
            "EMBED_BASE_URL / EMBED_MODEL in .env to enable hybrid recall."
        )
    return OpenAI(api_key=settings.embed_api_key, base_url=settings.embed_base_url)


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Batch-embed a list of texts, preserving input order.

    Requests are chunked to _MAX_BATCH inputs to respect provider limits.
    """
    if not texts:
        return []
    settings = get_settings()
    client = _client()
    out: List[List[float]] = []
    for start in range(0, len(texts), _MAX_BATCH):
        chunk = texts[start : start + _MAX_BATCH]
        resp = client.embeddings.create(model=settings.embed_model, input=chunk)
        ordered = sorted(resp.data, key=lambda d: d.index)
        out.extend(list(d.embedding) for d in ordered)
    return out


def embed_query(text: str) -> List[float]:
    """Embed a single query string."""
    return embed_texts([text])[0]
