"""LLM extraction: build the fill-in prompt, call the model, parse + validate JSON."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .config import get_settings
from .ingest import Document
from .schema import (
    NODE_TYPE_NAMES,
    edge_endpoints_ok,
    schema_prompt_block,
)

# Edge provenance labels (borrowed from Graphify). LLM-extracted edges default
# to INFERRED; a future code (tree-sitter) extractor will emit EXTRACTED.
CONFIDENCE_LEVELS = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
DEFAULT_CONFIDENCE = "INFERRED"


class ExtractedNode(BaseModel):
    name: str
    type: str
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


def _load_prompt_template() -> str:
    return (
        resources.files("ame_kb.prompts")
        .joinpath("extract_v2.txt")
        .read_text(encoding="utf-8")
    )


def build_prompt(doc: Document) -> str:
    template = _load_prompt_template()
    return (
        template.replace("{{schema_block}}", schema_prompt_block())
        .replace("{{doc_id}}", doc.doc_id)
        .replace("{{doc_content}}", doc.numbered_text())
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
    """Keep nodes/edges that satisfy the fixed type schema; record drops.

    Semi-dynamic (V2): node/edge *types* stay fixed, but extra properties the
    LLM emits are kept in the properties JSON fallback instead of being dropped.
    """
    result = ExtractionResult(doc_id=doc_id)
    node_type_by_name: Dict[str, str] = {}

    for item in payload.get("nodes", []) or []:
        try:
            node = ExtractedNode(**item)
        except ValidationError:
            result.dropped.append(f"node parse error: {item!r}")
            continue
        if node.type not in NODE_TYPE_NAMES:
            result.dropped.append(f"node bad type '{node.type}': {node.name}")
            continue
        # Keep all properties (schema-declared + extra) as JSON fallback.
        result.nodes.append(node)
        node_type_by_name[node.name] = node.type

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
        src_type = node_type_by_name.get(edge.source_name)
        dst_type = node_type_by_name.get(edge.target_name)
        if src_type is None or dst_type is None:
            result.dropped.append(
                f"edge endpoint not a known node: {edge.source_name}->{edge.target_name}"
            )
            continue
        if not edge_endpoints_ok(edge.label, src_type, dst_type):
            result.dropped.append(
                f"edge bad label/endpoints '{edge.label}': {src_type}->{dst_type}"
            )
            continue
        result.edges.append(edge)

    return result


def extract(doc: Document) -> ExtractionResult:
    prompt = build_prompt(doc)
    raw = call_llm(prompt)
    payload = _extract_json(raw)
    return validate(doc.doc_id, payload)
