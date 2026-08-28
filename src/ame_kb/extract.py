"""LLM extraction: build the fill-in prompt, call the model, parse + validate JSON."""
from __future__ import annotations

import json
import re
import hashlib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from importlib import resources
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

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
    # mention_id is a document-local locator used by extracted edges.  It is
    # deliberately not the durable graph identity: LLM-generated IDs can drift
    # between runs.
    mention_id: Optional[str] = None
    name: str
    type: str
    properties: Dict[str, object] = Field(default_factory=dict)
    source: List[str] = Field(default_factory=list)


class ExtractedEdge(BaseModel):
    source_mention_id: Optional[str] = None
    target_mention_id: Optional[str] = None
    # Kept for parsing V1/V2 model output.  validate() resolves these only when
    # the name identifies exactly one retained node, then fills mention IDs.
    source_name: Optional[str] = None
    target_name: Optional[str] = None
    label: str
    confidence: str = DEFAULT_CONFIDENCE
    source: List[str] = Field(default_factory=list)


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


def _normalize_name(name: str) -> str:
    """Return the stable comparison form used for document-local identity."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name).strip()).casefold()


def _mention_identity(type_: str, name: str) -> Tuple[str, str]:
    return type_, _normalize_name(name)


def _normalize_mention_id(value: Optional[str]) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", "-", normalized)


def _generated_mention_id(type_: str, name: str) -> str:
    """Generate a repeatable document-local locator for legacy node output."""
    identity = f"{type_}\0{_normalize_name(name)}"
    return f"m-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def _edge_endpoint_label(edge: ExtractedEdge) -> str:
    source = edge.source_mention_id or edge.source_name or "?"
    target = edge.target_mention_id or edge.target_name or "?"
    return f"{source}->{target}"


def validate(doc_id: str, payload: dict) -> ExtractionResult:
    """Keep nodes/edges that satisfy the fixed type schema; record drops.

    Semi-dynamic (V2): node/edge *types* stay fixed, but extra properties the
    LLM emits are kept in the properties JSON fallback instead of being dropped.
    """
    result = ExtractionResult(doc_id=doc_id)
    if not isinstance(payload, dict):
        result.dropped.append(f"payload is not an object: {payload!r}")
        return result

    candidates: List[Tuple[int, ExtractedNode]] = []
    for index, item in enumerate(payload.get("nodes", []) or []):
        try:
            node = ExtractedNode(**item)
        except (TypeError, ValidationError):
            result.dropped.append(f"node parse error: {item!r}")
            continue
        if not node.name.strip():
            result.dropped.append(f"node empty name: {item!r}")
            continue
        if node.type not in NODE_TYPE_NAMES:
            result.dropped.append(f"node bad type '{node.type}': {node.name}")
            continue
        node.mention_id = (
            _normalize_mention_id(node.mention_id)
            or _generated_mention_id(node.type, node.name)
        )
        candidates.append((index, node))

    # mention_key is based on type + normalized name, not mention_id.  If the
    # same business mention occurs twice we cannot assign a stable contribution
    # identity across forced reruns, even when this particular LLM response gave
    # the copies different IDs.  Drop every member instead of linking at random.
    identity_counts = Counter(
        _mention_identity(node.type, node.name) for _, node in candidates
    )
    mention_id_counts = Counter(node.mention_id for _, node in candidates)
    for _, node in candidates:
        identity = _mention_identity(node.type, node.name)
        if identity_counts[identity] > 1:
            result.dropped.append(
                f"node ambiguous duplicate '{node.type}:{identity[1]}': {node.name}"
            )
            continue
        if mention_id_counts[node.mention_id] > 1:
            result.dropped.append(
                f"node duplicate mention_id '{node.mention_id}': {node.name}"
            )
            continue
        # Keep all properties (schema-declared + extra) as JSON fallback.
        result.nodes.append(node)

    node_by_mention_id = {node.mention_id: node for node in result.nodes}
    nodes_by_name: Dict[str, List[ExtractedNode]] = defaultdict(list)
    for node in result.nodes:
        nodes_by_name[_normalize_name(node.name)].append(node)

    def resolve_endpoint(
        mention_id: Optional[str], name: Optional[str]
    ) -> Tuple[Optional[ExtractedNode], Optional[str]]:
        if mention_id is not None:
            normalized_id = _normalize_mention_id(mention_id)
            node = node_by_mention_id.get(normalized_id)
            if node is None:
                return None, f"unknown mention_id '{normalized_id or mention_id}'"
            if name is not None and _normalize_name(name) != _normalize_name(node.name):
                return None, f"mention_id/name mismatch '{normalized_id}'/'{name}'"
            return node, None
        if name is None or not name.strip():
            return None, "missing mention_id and legacy name"
        matches = nodes_by_name.get(_normalize_name(name), [])
        if len(matches) != 1:
            reason = "ambiguous" if len(matches) > 1 else "unknown"
            return None, f"{reason} legacy name '{name}'"
        return matches[0], None

    for item in payload.get("edges", []) or []:
        try:
            edge = ExtractedEdge(**item)
        except (TypeError, ValidationError):
            result.dropped.append(f"edge parse error: {item!r}")
            continue
        if edge.confidence not in CONFIDENCE_LEVELS:
            result.dropped.append(
                f"edge bad confidence '{edge.confidence}': "
                f"{_edge_endpoint_label(edge)}"
            )
            continue
        src_node, src_error = resolve_endpoint(
            edge.source_mention_id, edge.source_name
        )
        dst_node, dst_error = resolve_endpoint(
            edge.target_mention_id, edge.target_name
        )
        if src_node is None or dst_node is None:
            details = "; ".join(x for x in (src_error, dst_error) if x)
            result.dropped.append(
                f"edge endpoint not a unique known node: "
                f"{_edge_endpoint_label(edge)} ({details})"
            )
            continue
        if not edge_endpoints_ok(edge.label, src_node.type, dst_node.type):
            result.dropped.append(
                f"edge bad label/endpoints '{edge.label}': "
                f"{src_node.type}->{dst_node.type}"
            )
            continue
        edge.source_mention_id = src_node.mention_id
        edge.target_mention_id = dst_node.mention_id
        # Populate legacy display fields too so existing CLI/consumers continue
        # to work while switching their serialized contract to mention IDs.
        edge.source_name = src_node.name
        edge.target_name = dst_node.name
        result.edges.append(edge)

    return result


def extract(doc: Document) -> ExtractionResult:
    prompt = build_prompt(doc)
    raw = call_llm(prompt)
    payload = _extract_json(raw)
    return validate(doc.doc_id, payload)
