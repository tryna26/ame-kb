"""OpenAI-compatible embeddings and GraphNode cache management."""
from __future__ import annotations

import hashlib
import json
from numbers import Real
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from openai import OpenAI

from .config import get_settings
from .vecmath import validate_vector

_MAX_BATCH = 100


def _setting(settings: object, name: str, fallback: str = "") -> str:
    value = getattr(settings, name, None)
    return str(value) if value not in (None, "") else str(fallback)


def embedding_config() -> Tuple[str, str, str]:
    """Return API key, base URL and the explicitly configured model.

    Credentials/endpoint may reuse the chat gateway, but the model may not:
    chat models commonly do not expose the embeddings operation.
    """

    settings = get_settings()
    return (
        _setting(settings, "embed_api_key", getattr(settings, "llm_api_key", "")),
        _setting(settings, "embed_base_url", getattr(settings, "llm_base_url", "")),
        _setting(settings, "embed_model"),
    )


def embedding_available() -> bool:
    return all(embedding_config())


def _client() -> OpenAI:
    api_key, base_url, _model = embedding_config()
    if not api_key or not base_url:
        raise RuntimeError("Embedding endpoint is not configured")
    return OpenAI(api_key=api_key, base_url=base_url)


def _json_text(value: object) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def embedding_text(node: object) -> str:
    """Build stable semantic text: ``name|type|description/properties``."""

    name = str(getattr(node, "name", "") or "").strip()
    type_ = str(getattr(node, "type", "") or "").strip()
    description = str(getattr(node, "description", "") or "").strip()
    properties = _json_text(getattr(node, "properties", None))
    details = "\n".join(part for part in (description, properties) if part)
    return f"{name}|{type_}|{details}"


def embedding_hash(
    text: str, model: Optional[str] = None, base_url: Optional[str] = None
) -> str:
    """Hash endpoint, model identity and exact input text.

    Provider endpoints can expose equal model labels backed by incompatible
    vector spaces, so switching ``EMBED_BASE_URL`` must invalidate the cache.
    """

    if model is None:
        _api_key, configured_base_url, model = embedding_config()
        base_url = configured_base_url if base_url is None else base_url
    if not model:
        raise RuntimeError("EMBED_MODEL must be configured for entity resolution")
    if base_url is None:
        _api_key, base_url, _configured_model = embedding_config()
    raw = f"{base_url or ''}\0{model}\0{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _configured_dimension() -> Optional[int]:
    raw = getattr(get_settings(), "embed_dim", 0)
    dimension = int(raw or 0)
    if dimension < 0:
        raise ValueError("EMBED_DIM must not be negative")
    return dimension or None


def _validate_batch(
    data: Iterable[object],
    expected_count: int,
    *,
    expected_dimension: Optional[int] = None,
) -> List[List[float]]:
    rows = list(data)
    if len(rows) != expected_count:
        raise ValueError(
            "embedding response count mismatch: "
            f"expected {expected_count}, got {len(rows)}"
        )
    by_index: Dict[int, List[float]] = {}
    dimension: Optional[int] = None
    for row in rows:
        index = getattr(row, "index", None)
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("embedding response contains a non-integer index")
        if index < 0 or index >= expected_count or index in by_index:
            raise ValueError(f"embedding response contains invalid index {index!r}")
        raw_vector = getattr(row, "embedding", None)
        if not isinstance(raw_vector, Sequence) or isinstance(raw_vector, (str, bytes)):
            raise ValueError(f"embedding at index {index} is not a vector")
        # Validate provider values before conversion: ``bool`` is an ``int``
        # subclass and float(True) would otherwise silently turn it into 1.0.
        for value_index, value in enumerate(raw_vector):
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(
                    f"embedding[{index}][{value_index}] is not a number"
                )
        vector = [float(value) for value in raw_vector]
        required_dimension = expected_dimension or dimension
        validate_vector(vector, dimension=required_dimension)
        dimension = dimension or len(vector)
        by_index[index] = vector
    expected_indices = set(range(expected_count))
    if set(by_index) != expected_indices:
        raise ValueError("embedding response indices are incomplete")
    return [by_index[index] for index in range(expected_count)]


def embed_texts(texts: Sequence[str], *, batch_size: int = _MAX_BATCH) -> List[List[float]]:
    """Call a real OpenAI-compatible embeddings endpoint in batches.

    Provider output is reordered by ``index`` only after validating counts,
    unique/complete indices, a common non-zero dimension, and finite values.
    """

    values = list(texts)
    if not values:
        return []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    _api_key, base_url, model = embedding_config()
    if not model:
        raise RuntimeError("Embedding model is not configured")
    client = _client()
    result: List[List[float]] = []
    expected_dimension = _configured_dimension()
    for start in range(0, len(values), batch_size):
        chunk = values[start : start + batch_size]
        response = client.embeddings.create(model=model, input=chunk)
        vectors = _validate_batch(
            response.data, len(chunk), expected_dimension=expected_dimension
        )
        for vector in vectors:
            expected_dimension = expected_dimension or len(vector)
            validate_vector(vector, dimension=expected_dimension)
        result.extend(vectors)
    if len(result) != len(values):  # defensive invariant across chunks
        raise ValueError("embedding response count mismatch after batching")
    return result


def embed_query(text: str) -> List[float]:
    return embed_texts([text])[0]


def ensure_embeddings(nodes: Sequence[object]) -> List[List[float]]:
    """Return valid embeddings for nodes and update stale ORM cache fields.

    Cache validity requires a matching content/model hash, matching model name,
    and a structurally valid vector.  Dirty ORM objects are intentionally not
    committed here; their caller owns the transaction.
    """

    nodes = list(nodes)
    if not nodes:
        return []
    _api_key, base_url, model = embedding_config()
    if not model:
        raise RuntimeError("EMBED_MODEL must be configured for entity resolution")
    configured_dimension = _configured_dimension()
    texts = [embedding_text(node) for node in nodes]
    hashes = [embedding_hash(text, model, base_url) for text in texts]
    vectors: List[Optional[List[float]]] = [None] * len(nodes)
    missing: List[int] = []
    common_dimension: Optional[int] = configured_dimension

    for index, (node, digest) in enumerate(zip(nodes, hashes)):
        cached = getattr(node, "embedding", None)
        if (
            cached is None
            or not isinstance(cached, Sequence)
            or isinstance(cached, (str, bytes))
            or getattr(node, "embedding_hash", None) != digest
            or getattr(node, "embedding_model", None) != model
        ):
            missing.append(index)
            continue
        try:
            validate_vector(cached, dimension=common_dimension)
        except (TypeError, ValueError):
            missing.append(index)
            continue
        vector = [float(value) for value in cached]
        common_dimension = common_dimension or len(vector)
        vectors[index] = vector

    if missing:
        generated = embed_texts([texts[index] for index in missing])
        generated_dimension = len(generated[0])
        if common_dimension is not None and generated_dimension != common_dimension:
            # A same-model cache with a different dimension is corrupt/stale.
            # Refresh the complete set so all returned vectors share one shape.
            missing = list(range(len(nodes)))
            generated = embed_texts(texts)
        for index, vector in zip(missing, generated):
            node = nodes[index]
            node.embedding = vector
            node.embedding_hash = hashes[index]
            node.embedding_model = model
            vectors[index] = vector

    final = [vector for vector in vectors if vector is not None]
    if len(final) != len(nodes):
        raise RuntimeError("failed to populate every node embedding")
    dimension = len(final[0])
    for vector in final:
        validate_vector(vector, dimension=dimension)
    return final


def ensure_embedding(node: object) -> List[float]:
    """Single-node convenience wrapper around :func:`ensure_embeddings`."""

    return ensure_embeddings([node])[0]
