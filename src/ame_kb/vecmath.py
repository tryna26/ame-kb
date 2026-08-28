"""Small, strict vector helpers used by entity resolution.

The resolver treats a malformed embedding as data corruption rather than as a
zero-similarity candidate.  Keeping that rule here gives callers one shared
validation boundary.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Optional, Sequence


def validate_vector(
    vector: Sequence[float], *, dimension: Optional[int] = None
) -> None:
    """Raise ``ValueError`` unless *vector* is non-empty and finite.

    ``bool`` is deliberately rejected even though it is an ``int`` subclass:
    accepting ``true`` in an embedding JSON value hides provider/schema bugs.
    """

    if not vector:
        raise ValueError("embedding vector must not be empty")
    if dimension is not None and len(vector) != dimension:
        raise ValueError(
            f"embedding dimension mismatch: expected {dimension}, got {len(vector)}"
        )
    for index, value in enumerate(vector):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"embedding[{index}] is not a number")
        if not math.isfinite(float(value)):
            raise ValueError(f"embedding[{index}] is not finite")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Return cosine similarity for two valid equal-length vectors.

    Dimension mismatches, non-finite values, empty vectors, and zero-norm
    vectors are invalid inputs and raise ``ValueError``.  Silent ``0`` fallbacks
    would turn a broken embedding response into a misleading resolution result.
    """

    validate_vector(a)
    validate_vector(b, dimension=len(a))
    dot = math.fsum(float(x) * float(y) for x, y in zip(a, b))
    norm_a = math.sqrt(math.fsum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(math.fsum(float(y) * float(y) for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("cosine is undefined for a zero-norm vector")
    result = dot / (norm_a * norm_b)
    if not math.isfinite(result):
        raise ValueError("cosine result is not finite")
    # Floating point round-off can escape the mathematical [-1, 1] range by a
    # few ulps.  Clamp only after all validity checks have passed.
    return max(-1.0, min(1.0, result))
