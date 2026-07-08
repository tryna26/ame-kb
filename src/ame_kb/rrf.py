"""Reciprocal Rank Fusion (RRF), ported from general_recall/utils/rrf.go.

score(item) = Σ over lists of 1 / (k + rank), where rank is 1-based within each
list. Default k=60. Used both for per-channel text×vector fusion inside a single
recall call and for multi-query / multi-list fusion at the application layer.
"""
from __future__ import annotations

from typing import Callable, Dict, Hashable, List, Optional, Sequence, TypeVar

RRF_K = 60

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


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


def rrf_merge_objects(
    ranked_lists: Sequence[Sequence[T]],
    key_fn: Callable[[T], K],
    better_fn: Optional[Callable[[T, T], bool]] = None,
    top_k: int = 0,
    k: int = RRF_K,
) -> List[T]:
    """Generic RRF over objects, mirroring general_recall/utils/rrf.go RRFMerge.

    Dedup by key_fn(item); accumulate 1/(k+rank) across lists; keep one
    representative per key, replacing it when better_fn(new, current) is True.
    Sort by descending fused score (stable on first-seen order); truncate to
    top_k when > 0. Used for multi-query fusion where each query yields its own
    ranked result list.
    """
    scores: Dict[K, float] = {}
    best: Dict[K, T] = {}
    first_seen: Dict[K, int] = {}
    order = 0
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_fn(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in best:
                best[key] = item
                first_seen[key] = order
                order += 1
            elif better_fn is not None and better_fn(item, best[key]):
                best[key] = item
    ordered_keys = sorted(scores, key=lambda kk: (-scores[kk], first_seen[kk]))
    if top_k > 0:
        ordered_keys = ordered_keys[:top_k]
    return [best[kk] for kk in ordered_keys]
