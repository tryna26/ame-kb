"""Opt-in real MySQL + LLM + embedding smoke test for V3.

Run explicitly (it writes a fresh graph namespace):
    AME_KB_REAL_E2E=1 python tests/e2e_real_v3.py

Credentials come from the normal environment/.env and are never printed.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from sqlalchemy import func, select

from ame_kb.config import get_settings
from ame_kb.db import session_scope
from ame_kb.embed import ensure_embeddings
from ame_kb.extract import extract
from ame_kb.ingest import Document
from ame_kb.migrations import ensure_v3_schema
from ame_kb.models import GraphEdge, GraphNode, MergeLog, NodeContribution
from ame_kb.query import find_entities, relations_of
import ame_kb.resolve as resolver
from ame_kb.store import is_unchanged, store_document
from ame_kb.vecmath import cosine


def main() -> None:
    if os.getenv("AME_KB_REAL_E2E") != "1":
        raise SystemExit("set AME_KB_REAL_E2E=1 to allow real external writes")

    graph_no = os.getenv("AME_KB_E2E_GRAPH_NO") or f"e2e_v3_{uuid.uuid4().hex[:12]}"
    os.environ["GRAPH_NO"] = graph_no
    os.environ["GRAPH_VERSION"] = "1"
    os.environ["SOURCE_ID"] = "ame-kb-real-e2e"
    # Force semantically similar pairs through the real LLM judge.
    os.environ["RESOLVE_LOW_THRESHOLD"] = "0.0"
    os.environ["RESOLVE_HIGH_THRESHOLD"] = "0.999999"
    get_settings.cache_clear()
    settings = get_settings()

    ensure_v3_schema()
    ensure_v3_schema()
    fixture_dir = Path(__file__).parent / "fixtures" / "v3_e2e"
    documents = [
        Document(path.name, path, path.read_text(encoding="utf-8"))
        for path in sorted(fixture_dir.glob("*.md"))
    ]
    extraction_counts = {}
    for document in documents:
        result = extract(document)
        store_document(result, document, source_id=settings.source_id)
        assert is_unchanged(document, source_id=settings.source_id)
        extraction_counts[document.doc_id] = {
            "nodes": len(result.nodes),
            "edges": len(result.edges),
            "dropped": len(result.dropped),
        }

    with session_scope() as session:
        people = list(
            session.scalars(
                select(GraphNode).where(
                    GraphNode.graph_no == graph_no,
                    GraphNode.type == "Person",
                    GraphNode.deleted == 0,
                )
            )
        )
        ada = [node for node in people if "Ada" in node.name or "阿达" in node.name]
        assert len(ada) == 2
        vectors = ensure_embeddings(ada)
        similarity = cosine(vectors[0], vectors[1])

    judge_calls = 0
    original = resolver.call_llm

    def counted(prompt: str) -> str:
        nonlocal judge_calls
        judge_calls += 1
        return original(prompt)

    resolver.call_llm = counted
    try:
        first = resolver.resolve_all()
    finally:
        resolver.call_llm = original
    assert first.llm_merged >= 1 and judge_calls > 0
    assert resolver.resolve_all().merged == 0

    hits = find_entities("Ada Lovelace")
    assert len(hits) == 1
    assert any(relation.label == "authored" for relation in relations_of(hits[0].graph_node_no))

    with session_scope() as session:
        logs = list(
            session.scalars(
                select(MergeLog)
                .where(MergeLog.graph_no == graph_no, MergeLog.status == "MERGED")
                .order_by(MergeLog.id.desc())
            )
        )
        counts = {
            "nodes": session.scalar(
                select(func.count()).select_from(GraphNode).where(GraphNode.graph_no == graph_no)
            ),
            "edges": session.scalar(
                select(func.count()).select_from(GraphEdge).where(GraphEdge.graph_no == graph_no)
            ),
            "node_contributions": session.scalar(
                select(func.count()).select_from(NodeContribution).where(NodeContribution.graph_no == graph_no)
            ),
        }
    for log in logs:
        resolver.rollback(log.merge_id)

    print(
        json.dumps(
            {
                "status": "PASS",
                "graph_no": graph_no,
                "extractions": extraction_counts,
                "ada_cosine": similarity,
                "judge_calls": judge_calls,
                "merges": first.merged,
                "counts": counts,
                "rollbacks": len(logs),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
