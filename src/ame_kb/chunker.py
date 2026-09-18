"""Document chunking for the doc-chunk retrieval channel.

Sliding window over the text, preferring to end a chunk at a newline boundary
past the window midpoint for cleaner splits (general_recall/core/entity/
sync_spec_doc_chunk.go:130 splitTextIntoChunks). Each chunk records the 1-based
line_start/line_end it spans so a hit can be traced back to kg_doc_line, matching
the Ref-provenance model used elsewhere.

Character-based windowing is used (works for CJK without a tokenizer); size and
overlap come from CHUNK_SIZE / CHUNK_OVERLAP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class Chunk:
    chunk_index: int
    content: str
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive


def _line_starts(text: str) -> List[int]:
    """Character offset at which each 1-based line begins."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of_offset(line_starts: List[int], offset: int) -> int:
    """1-based line number containing character `offset` (binary search)."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def split_text(text: str, chunk_size: int, overlap: int) -> List[Chunk]:
    """Split `text` into overlapping chunks with line spans.

    Empty/whitespace-only text yields no chunks. Guards against non-progress
    when overlap >= chunk_size.
    """
    if not text or not text.strip():
        return []
    if chunk_size <= 0:
        chunk_size = len(text)
    overlap = max(0, min(overlap, chunk_size - 1))

    line_starts = _line_starts(text)
    n = len(text)
    chunks: List[Chunk] = []
    start = 0
    idx = 0
    while start < n:
        end = min(start + chunk_size, n)
        # Prefer to break at a newline past the midpoint for a cleaner boundary.
        if end < n:
            nl = text.rfind("\n", start, end)
            if nl != -1 and nl > start + chunk_size // 2:
                end = nl
        piece = text[start:end]
        if piece.strip():
            chunks.append(
                Chunk(
                    chunk_index=idx,
                    content=piece.strip("\n"),
                    line_start=_line_of_offset(line_starts, start),
                    line_end=_line_of_offset(line_starts, max(start, end - 1)),
                )
            )
            idx += 1
        if end >= n:
            break
        nxt = end - overlap
        start = nxt if nxt > start else end  # never stall
    return chunks
