"""Pure vector math shared by the MySQL search backend and recall tests.

Kept dependency-free so both `recall` and `searchbackend.mysql` can import it
without creating an import cycle.
"""
from __future__ import annotations

import math
from typing import Sequence


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0 if either is empty/zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
