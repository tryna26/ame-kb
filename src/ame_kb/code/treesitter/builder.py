"""Graph accumulator + finalisation shared by all language extractors.

Mirrors code2skill analyzer/treesitter/builder.go: a GraphBuilder collects
nodes and dedup'd edges during per-file extraction, then finalize() derives the
package hierarchy (namespace nodes for intermediate dirs), topo-sorts packages
dependency-first, and computes in/out degrees. build_code_graph() dispatches to
each requested language and merges everything into one CodeGraph.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Set, Tuple

from ..codegraph import CodeGraph, EdgeKind, GEdge, GNode, NodeKind

REPO_NODE_ID = "repository"


@dataclass
class FileResult:
    """Per-file extraction output, shared across language extractors."""

    file_node: GNode
    symbols: List[GNode] = field(default_factory=list)
    raw_imports: List[str] = field(default_factory=list)


class GraphBuilder:
    def __init__(self) -> None:
        self.nodes: List[GNode] = []
        self.edges: List[GEdge] = []
        self._edge_set: Set[str] = set()

    def add_node(self, node: GNode) -> None:
        self.nodes.append(node)

    def add_edge(self, frm: str, to: str, kind: EdgeKind) -> None:
        key = f"{frm}|{kind.value}|{to}"
        if key in self._edge_set:
            return
        self._edge_set.add(key)
        self.edges.append(GEdge(from_=frm, to=to, kind=kind))

    def finalize(self, repo_id: str, pkg_ids: Set[str]) -> CodeGraph:
        self.nodes, self.edges = derive_package_hierarchy(
            self.nodes, self.edges, pkg_ids, self._edge_set
        )
        topo_order, cycles = topo_sort(self.nodes, self.edges)

        node_by_id = {n.id: n for n in self.nodes}
        for e in self.edges:
            if e.from_ in node_by_id:
                node_by_id[e.from_].out_degree += 1
            if e.to in node_by_id:
                node_by_id[e.to].in_degree += 1

        return CodeGraph(
            repo_id=repo_id,
            nodes=self.nodes,
            edges=self.edges,
            cycles=cycles,
            topo_order=topo_order,
        )


# ── Package hierarchy ──────────────────────────────────────────────────────────


def parent_pkg(pkg_id: str) -> str:
    """Parent directory package ID. Returns '' for top-level."""
    if pkg_id == "." or "/" not in pkg_id:
        return ""
    idx = pkg_id.rfind("/")
    if idx <= 0:
        return "."
    return pkg_id[:idx]


def derive_package_hierarchy(
    nodes: List[GNode],
    edges: List[GEdge],
    pkg_ids: Set[str],
    edge_set: Set[str],
) -> Tuple[List[GNode], List[GEdge]]:
    """Insert namespace package nodes for intermediate dirs + SubPackage edges."""

    def add_edge(frm: str, to: str, kind: EdgeKind) -> None:
        key = f"{frm}|{kind.value}|{to}"
        if key in edge_set:
            return
        edge_set.add(key)
        edges.append(GEdge(from_=frm, to=to, kind=kind))

    # Collect all ancestor prefixes of real packages.
    all_dirs: Set[str] = set()
    for pid in list(pkg_ids):
        parts = pid.split("/")
        for i in range(len(parts)):
            all_dirs.add("/".join(parts[: i + 1]))
        if pid == ".":
            all_dirs.add(".")

    for pid in list(pkg_ids):
        parent = parent_pkg(pid)
        if parent == "":
            add_edge(REPO_NODE_ID, pid, EdgeKind.SUBPACKAGE)
            continue
        if parent not in pkg_ids and parent in all_dirs:
            nodes.append(
                GNode(
                    id=parent,
                    kind=NodeKind.PACKAGE,
                    name=parent,
                    import_path=parent,
                )
            )
            pkg_ids.add(parent)
            add_edge(REPO_NODE_ID, parent, EdgeKind.CONTAINS)
            add_edge(REPO_NODE_ID, parent, EdgeKind.SUBPACKAGE)
        add_edge(parent, pid, EdgeKind.SUBPACKAGE)

    return nodes, edges


# ── Topo sort (packages only, dependency-first) ─────────────────────────────────


def topo_sort(
    nodes: List[GNode], edges: List[GEdge]
) -> Tuple[List[str], List[List[str]]]:
    pkg_set = {n.id for n in nodes if n.kind == NodeKind.PACKAGE}

    in_deg: Dict[str, int] = {pid: 0 for pid in pkg_set}
    adj: Dict[str, List[str]] = {pid: [] for pid in pkg_set}

    for e in edges:
        if e.from_ not in pkg_set or e.to not in pkg_set:
            continue
        if e.kind in (EdgeKind.IMPORT, EdgeKind.SUBPACKAGE):
            # Dependency points to->from: process dependency (to) before dependent (from).
            adj[e.to].append(e.from_)
            in_deg[e.from_] += 1

    queue = sorted([pid for pid, d in in_deg.items() if d == 0])
    order: List[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        nexts = sorted(adj[cur])
        new_items: List[str] = []
        for nxt in nexts:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                new_items.append(nxt)
        queue.extend(sorted(new_items))

    cycles = [[pid] for pid, d in in_deg.items() if d > 0]

    # Non-package nodes trail the package order (stable by appearance).
    for n in nodes:
        if n.kind != NodeKind.PACKAGE:
            order.append(n.id)

    return order, cycles


# ── Language dispatch ───────────────────────────────────────────────────────────


def build_code_graph(repo_path: str, langs: List[str]) -> CodeGraph:
    """Extract a CodeGraph for the given languages, merged into one graph."""
    from . import lang_go, lang_python

    builders: Dict[str, Callable[[str, GraphBuilder], Set[str]]] = {
        "python": lang_python.build_python,
        "go": lang_go.build_go,
    }

    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    repo_id = os.path.basename(repo_path.rstrip("/"))

    gb = GraphBuilder()
    gb.add_node(GNode(id=REPO_NODE_ID, kind=NodeKind.REPOSITORY, name=repo_id))

    all_pkg_ids: Set[str] = set()
    for lang in langs:
        builder = builders.get(lang)
        if builder is None:
            raise ValueError(
                f"unsupported language {lang!r} — supported: {', '.join(sorted(builders))}"
            )
        all_pkg_ids |= builder(repo_path, gb)

    if not all_pkg_ids:
        raise ValueError(
            f"no source files found in {repo_path} for languages: {', '.join(langs)}"
        )

    return gb.finalize(repo_id, all_pkg_ids)
