"""LLM extraction: build the fill-in prompt, call the model, parse + validate JSON."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .config import get_settings
from .ingest import Document
from .schema import (
    ENTITY_SPECS,
    ENTITY_TYPES,
    schema_prompt_block,
)
from .srcclass import SourceKind, classify_source

# Prompt template per source kind. CODE is routed to the generic prompt for now:
# the tree-sitter structural extractor is not built yet, so code files fall back
# to LLM extraction until codeextract.py lands (see ROADMAP V5/V6).
_PROMPT_BY_KIND: Dict[SourceKind, str] = {
    SourceKind.DOC: "extract_v2.txt",
    SourceKind.PLAN: "extract_plan.txt",
    SourceKind.CODE: "extract_v2.txt",  # TODO: swap for tree-sitter extractor
}

# Edge provenance labels (borrowed from Graphify). LLM-extracted edges default
# to INFERRED; a future code (tree-sitter) extractor will emit EXTRACTED.
CONFIDENCE_LEVELS = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
DEFAULT_CONFIDENCE = "INFERRED"


class ExtractedNode(BaseModel):
    name: str
    entity_type: str
    entity_spec: Optional[str] = None
    description: str = ""
    properties: Dict[str, object] = {}
    source: List[str] = []


class ExtractedEdge(BaseModel):
    source_name: str
    target_name: str
    label: str
    description: str = ""
    confidence: str = DEFAULT_CONFIDENCE
    source: List[str] = []


@dataclass
class ExtractionResult:
    doc_id: str
    nodes: List[ExtractedNode] = field(default_factory=list)
    edges: List[ExtractedEdge] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)


def _load_prompt_template(template_name: str = "extract_v2.txt") -> str:
    return (
        resources.files("ame_kb.prompts")
        .joinpath(template_name)
        .read_text(encoding="utf-8")
    )


def build_prompt(doc: Document) -> str:
    template = _load_prompt_template(_PROMPT_BY_KIND[classify_source(doc)])
    return (
        template.replace("{{schema_block}}", schema_prompt_block())
        .replace("{{doc_id}}", doc.doc_id)
        .replace("{{doc_content}}", doc.numbered_text())
    )


def _build_prompt_text(
    doc_id: str, numbered_text: str, template_name: str = "extract_v2.txt"
) -> str:
    template = _load_prompt_template(template_name)
    return (
        template.replace("{{schema_block}}", schema_prompt_block())
        .replace("{{doc_id}}", doc_id)
        .replace("{{doc_content}}", numbered_text)
    )


def _extract_json(raw: str) -> dict:
    """Parse a JSON object from the model output, tolerating code fences."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _client() -> OpenAI:
    settings = get_settings()
    return OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)


def call_llm(prompt: str) -> str:
    settings = get_settings()
    # This gateway streams responses (SSE), so consume the stream and join deltas.
    stream = _client().chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        stream=True,
    )
    parts: List[str] = []
    for chunk in stream:
        if not chunk.choices:
            continue
        piece = chunk.choices[0].delta.content
        if piece:
            parts.append(piece)
    return "".join(parts)


def validate(doc_id: str, payload: dict) -> ExtractionResult:
    """Keep nodes/edges that satisfy the fixed ontology; record drops.

    A node is kept iff its entity_type is a valid metatype and, when the
    metatype is Asset, its entity_spec is a valid archetype. Non-Asset nodes
    must not carry an archetype. An edge is kept iff its confidence is valid and
    both endpoints reference a kept node (referential integrity).
    """
    result = ExtractionResult(doc_id=doc_id)
    node_type_by_name: Dict[str, str] = {}

    for item in payload.get("nodes", []) or []:
        try:
            node = ExtractedNode(**item)
        except ValidationError:
            result.dropped.append(f"node parse error: {item!r}")
            continue
        if not node.name:
            result.dropped.append(f"node empty name: {item!r}")
            continue
        if node.entity_type not in ENTITY_TYPES:
            result.dropped.append(
                f"node bad entity_type '{node.entity_type}': {node.name}"
            )
            continue
        if node.entity_type == "Asset":
            if node.entity_spec not in ENTITY_SPECS:
                result.dropped.append(
                    f"node bad entity_spec '{node.entity_spec}': {node.name}"
                )
                continue
        else:
            node.entity_spec = None
        # Keep all properties (schema-declared + extra) as JSON fallback.
        result.nodes.append(node)
        node_type_by_name[node.name] = node.entity_type

    for item in payload.get("edges", []) or []:
        try:
            edge = ExtractedEdge(**item)
        except ValidationError:
            result.dropped.append(f"edge parse error: {item!r}")
            continue
        if edge.confidence not in CONFIDENCE_LEVELS:
            result.dropped.append(
                f"edge bad confidence '{edge.confidence}': "
                f"{edge.source_name}->{edge.target_name}"
            )
            continue
        if (
            edge.source_name not in node_type_by_name
            or edge.target_name not in node_type_by_name
        ):
            result.dropped.append(
                f"edge endpoint not a known node: {edge.source_name}->{edge.target_name}"
            )
            continue
        result.edges.append(edge)

    return result


def _extract_span(
    doc_id: str, numbered_lines: List[str], template_name: str = "extract_v2.txt"
) -> ExtractionResult:
    """Extract from a numbered-line span, splitting in half and retrying if the
    model returns unparseable JSON (Graphify's invalid-JSON recovery). Bottoms
    out at a single line to avoid unbounded recursion."""
    numbered_text = "\n".join(numbered_lines)
    prompt = _build_prompt_text(doc_id, numbered_text, template_name)
    raw = call_llm(prompt)
    try:
        payload = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        if len(numbered_lines) <= 1:
            res = ExtractionResult(doc_id=doc_id)
            res.dropped.append("json parse error on single line; skipped")
            return res
        mid = len(numbered_lines) // 2
        left = _extract_span(doc_id, numbered_lines[:mid], template_name)
        right = _extract_span(doc_id, numbered_lines[mid:], template_name)
        left.nodes.extend(right.nodes)
        left.edges.extend(right.edges)
        left.dropped.extend(right.dropped)
        return left
    return validate(doc_id, payload)


def extract(doc: Document) -> ExtractionResult:
    template_name = _PROMPT_BY_KIND[classify_source(doc)]
    numbered_lines = doc.numbered_text().splitlines()
    return _extract_span(doc.doc_id, numbered_lines, template_name)
