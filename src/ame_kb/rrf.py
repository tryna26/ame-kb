"""Reciprocal Rank Fusion (RRF), ported from general_recall/utils/rrf.go.

score(item) = Σ over lists of 1 / (k + rank), where rank is 1-based within each
list. Default k=60. Used both for per-channel text×vector fusion inside a single
recall call and for multi-query / multi-list fusion at the application layer.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

RRF_K = 60


def rrf_merge(ranked_lists: Sequence[Sequence[str]], k: int = RRF_K) -> List[str]:
    """Fuse several ranked id lists into one, sorted by descending RRF score.

    Each inner list is an ordered sequence of ids (best first). Ties preserve the
    order in which an id was first seen across the input lists (stable).
    """
    scores: Dict[str, float] = {}
    first_seen: Dict[str, int] = {}
    order = 0
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    return sorted(scores, key=lambda i: (-scores[i], first_seen[i]))
