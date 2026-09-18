"""Multi-query expansion: rewrite one user query into several search queries.

Mirrors general_recall/application/recall/method/rag_retrieve.go: each query is
searched independently, then the ranked result lists are fused with RRF. Off by
default (RECALL_MAX_QUERIES=1 -> just the original query, no LLM call, so recall
stays byte-identical to V3). When enabled, an LLM proposes paraphrases /
sub-questions; failures fall back to the original query only.
"""
from __future__ import annotations

import json
import re
from typing import List

from .config import get_settings

_PROMPT = (
    "你是检索查询改写器。基于用户的问题，生成 {n} 个用于知识库检索的中文查询，"
    "覆盖同义表达、关键实体、以及可能的子问题。只输出 JSON 数组，例如 "
    '["查询1","查询2"]，不要解释。\n\n用户问题：{q}'
)


def _dedup_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        s = (it or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _parse_array(raw: str) -> List[str]:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, (str, int, float))]


def expand_queries(query: str, warnings: List[str]) -> List[str]:
    """Return [original] + LLM rewrites, capped at RECALL_MAX_QUERIES.

    The original query is always first and always kept. On any error we return
    just [original] and record a warning.
    """
    settings = get_settings()
    max_q = max(1, settings.recall_max_queries)
    if max_q <= 1:
        return [query]

    try:
        from .extract import call_llm

        prompt = _PROMPT.format(n=max_q - 1, q=query)
        raw = call_llm(prompt)
        extra = _parse_array(raw)
    except Exception as exc:  # noqa: BLE001 - expansion is best-effort
        warnings.append(f"query expansion failed: {exc}")
        extra = []

    return _dedup_keep_order([query, *extra])[:max_q]
