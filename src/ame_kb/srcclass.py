"""Source classification: route each document to the right extraction strategy.

Extraction quality depends on the source. A project plan, a generic doc, and a
code file each want a different prompt (or, for code, a different extractor
entirely). This module maps a Document to a coarse SourceKind so extract.route()
can pick the strategy. It is intentionally simple and explainable: file suffix
first (most certain), then filename/doc_id keyword heuristics, then a generic
fallback. Add new sources by extending the tables here, not by touching the
extraction pipeline.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ingest import Document


class SourceKind(str, Enum):
    """Extraction strategy bucket. Each maps to a prompt or extractor."""

    CODE = "code"  # tree-sitter structural extractor (placeholder for now)
    PLAN = "plan"  # project plans / design docs: milestones, deliverables, risks
    DOC = "doc"  # generic documents / web pages (current default prompt)


# Code file suffixes routed to the (future) structural extractor. Kept small on
# purpose; unknown code-ish suffixes fall through to LLM extraction as DOC.
CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cs",
        ".rb",
        ".php",
        ".kt",
        ".swift",
        ".scala",
        ".m",
        ".mm",
    }
)

# Keyword stems that mark a document as a project plan / design doc. Matched
# case-insensitively against the doc_id (relative path) and title, so both
# "docs/roadmap.md" and a file whose first heading is "项目计划书" are caught.
PLAN_KEYWORDS = (
    "plan",
    "roadmap",
    "prd",
    "design",
    "spec",
    "proposal",
    "milestone",
    "schedule",
    "计划",
    "规划",
    "路线图",
    "方案",
    "设计",
    "需求",
    "里程碑",
)


def _has_plan_keyword(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in PLAN_KEYWORDS)


def classify_source(doc: "Document") -> SourceKind:
    """Pick the extraction strategy for a document.

    Priority: code suffix (certain) -> plan keywords in path/title -> generic.
    """
    suffix = doc.path.suffix.lower()
    if suffix in CODE_SUFFIXES:
        return SourceKind.CODE

    if _has_plan_keyword(doc.doc_id) or _has_plan_keyword(doc.title):
        return SourceKind.PLAN

    return SourceKind.DOC
