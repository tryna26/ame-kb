"""Code-graph data model + JSON persistence (Phase 1, structural layer).

Mirrors code2skill's internal/cache/code_graph.go, trimmed to what ame-kb's
phase-1 code extraction needs. tree-sitter fills these nodes/edges with zero
LLM; the projection layer (code/project.py) maps them onto the ontology three
tables. GNode.summary stays empty in phase 1 and is filled by the phase-2
multi-agent wave summarizer.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional


class NodeKind(str, Enum):
    REPOSITORY = "repository"
    PACKAGE = "package"
    FILE = "file"
    FUNCTION = "function"
    METHOD = "method"
    STRUCT = "struct"
    INTERFACE = "interface"
    TYPE_ALIAS = "type_alias"


class EdgeKind(str, Enum):
    # Containment (tree-shaped): Repository->Package, Package->File, File->Symbol.
    CONTAINS = "Contains"
    # Cross-package import (DAG-shaped).
    IMPORT = "Import"
    # Parent dir -> child package (package hierarchy).
    SUBPACKAGE = "SubPackage"


@dataclass
class GNode:
    id: str  # unique: "pkg/QualifiedName" for symbols, relpath for files, pkgID for packages
    kind: NodeKind
    name: str
    import_path: str = ""  # package nodes
    signature: str = ""  # AST-extracted header, authoritative
    doc: str = ""  # original doc comment / docstring, verbatim
    file: str = ""  # relative path
    package: str = ""
    line: int = 0
    end_line: int = 0
    in_degree: int = 0
    out_degree: int = 0
    summarized: bool = False  # phase-2 wave summarizer flag
    summary: str = ""  # phase-2 LLM description


@dataclass
class GEdge:
    from_: str
    to: str
    kind: EdgeKind


@dataclass
class CodeGraph:
    repo_id: str
    nodes: List[GNode] = field(default_factory=list)
    edges: List[GEdge] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)
    topo_order: List[str] = field(default_factory=list)


def _node_to_dict(n: GNode) -> dict:
    d = asdict(n)
    d["kind"] = n.kind.value
    return d


def _edge_to_dict(e: GEdge) -> dict:
    return {"from": e.from_, "to": e.to, "kind": e.kind.value}


def to_json(graph: CodeGraph) -> str:
    payload = {
        "repo_id": graph.repo_id,
        "nodes": [_node_to_dict(n) for n in graph.nodes],
        "edges": [_edge_to_dict(e) for e in graph.edges],
        "cycles": graph.cycles,
        "topo_order": graph.topo_order,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def from_json(text: str) -> CodeGraph:
    payload = json.loads(text)
    nodes = [
        GNode(
            id=n["id"],
            kind=NodeKind(n["kind"]),
            name=n.get("name", ""),
            import_path=n.get("import_path", ""),
            signature=n.get("signature", ""),
            doc=n.get("doc", ""),
            file=n.get("file", ""),
            package=n.get("package", ""),
            line=n.get("line", 0),
            end_line=n.get("end_line", 0),
            in_degree=n.get("in_degree", 0),
            out_degree=n.get("out_degree", 0),
            summarized=n.get("summarized", False),
            summary=n.get("summary", ""),
        )
        for n in payload.get("nodes", [])
    ]
    edges = [
        GEdge(from_=e["from"], to=e["to"], kind=EdgeKind(e["kind"]))
        for e in payload.get("edges", [])
    ]
    return CodeGraph(
        repo_id=payload.get("repo_id", ""),
        nodes=nodes,
        edges=edges,
        cycles=payload.get("cycles", []),
        topo_order=payload.get("topo_order", []),
    )


def write_code_graph(cache_path: str, graph: CodeGraph) -> None:
    """Atomically write graph JSON to cache_path (parent dirs created)."""
    directory = os.path.dirname(cache_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(to_json(graph))
        os.replace(tmp, cache_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_code_graph(cache_path: str) -> Optional[CodeGraph]:
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r", encoding="utf-8") as f:
        return from_json(f.read())
