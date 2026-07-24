"""Orchestrate repository-level code ingestion (Phase 1).

ingest_repo() runs the structural pipeline: build the CodeGraph with tree-sitter
(zero LLM), cache it to disk for later phases, then project it onto the ontology
three tables. Phase 2 (multi-agent wave summarization) will slot between build
and project to fill GNode.summary.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from ..config import get_settings
from .codegraph import CodeGraph, write_code_graph
from .project import ProjectStats, project_code_graph
from .treesitter.builder import build_code_graph


@dataclass
class IngestReport:
    repo_id: str
    nodes: int
    edges: int
    cache_path: str
    stats: ProjectStats


def _cache_path_for(graph_no: str, repo_id: str) -> str:
    settings = get_settings()
    base = os.path.expanduser(getattr(settings, "code_cache_dir", "~/.ame-kb/code"))
    return os.path.join(base, graph_no, f"{repo_id}.code_graph.json")


def build_repo_graph(repo_path: str, langs: Optional[List[str]] = None) -> CodeGraph:
    settings = get_settings()
    if not langs:
        langs = [
            s.strip() for s in (settings.code_langs or "python,go").split(",") if s.strip()
        ]
    return build_code_graph(repo_path, langs)


def ingest_repo(
    repo_path: str, langs: Optional[List[str]] = None, *, project: bool = True
) -> IngestReport:
    settings = get_settings()
    graph = build_repo_graph(repo_path, langs)

    cache_path = _cache_path_for(settings.graph_no, graph.repo_id)
    if project:
        # Cache is an intermediate artifact for the real pipeline (phase-2 resume);
        # a dry-run stays side-effect-free and skips both the cache and the DB.
        write_code_graph(cache_path, graph)
        stats = project_code_graph(graph)
    else:
        stats = ProjectStats()

    return IngestReport(
        repo_id=graph.repo_id,
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        cache_path=cache_path if project else "(dry-run: not written)",
        stats=stats,
    )
