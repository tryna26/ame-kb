"""ame_kb.code — repository-level code extraction (Phase 1: tree-sitter structure).

Parses a code repository into a CodeGraph (functions/methods/types + package
imports) with zero LLM, then projects it onto the ontology three tables. See
code/ingest.py for the orchestration entry point ingest_repo().
"""
