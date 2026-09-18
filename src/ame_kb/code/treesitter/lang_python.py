"""Python extractor (tree-sitter). Ports code2skill analyzer/treesitter/lang_python.go.

Captures top-level functions, classes, and methods (functions nested in a class
body). Imports are resolved to in-repo package IDs. Signatures are the def/class
header without the body; docs are the leading docstring.
"""
from __future__ import annotations

import os
from typing import Dict, List, Set

from ..codegraph import EdgeKind, GNode, NodeKind
from .. import tsparse
from .builder import REPO_NODE_ID, FileResult, GraphBuilder

# function_definition and class_definition names are captured directly; methods
# are functions whose enclosing scope is a class body (resolved by walking up).
PY_SYMBOL_QUERY = """
(function_definition name: (identifier) @fn)
(decorated_definition definition: (function_definition name: (identifier) @fn))
(class_definition name: (identifier) @class)
(decorated_definition definition: (class_definition name: (identifier) @class))
"""

PY_IMPORT_QUERY = """
(import_statement name: (dotted_name) @import)
(import_from_statement module_name: (dotted_name) @import)
"""

PY_DIR_SKIP = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "dist",
    "build",
    ".eggs",
    "node_modules",
    ".tox",
}

_DECL_KINDS = {"function_definition", "class_definition", "decorated_definition"}
_BODY_KINDS = {"function_definition", "class_definition"}


def build_python(repo_path: str, gb: GraphBuilder) -> Set[str]:
    lang = tsparse.get_language("python")
    parser = tsparse.new_parser(lang)
    sym_query = tsparse.compile_query(lang, PY_SYMBOL_QUERY)
    imp_query = tsparse.compile_query(lang, PY_IMPORT_QUERY)

    pkg_files = discover_python_packages(repo_path)
    if not pkg_files:
        return set()
    pkg_ids: Set[str] = set(pkg_files.keys())

    pkg_imports: Dict[str, List[str]] = {}

    for pkg_id in sorted(pkg_files):
        gb.add_node(
            GNode(id=pkg_id, kind=NodeKind.PACKAGE, name=pkg_id, import_path=pkg_id)
        )
        gb.add_edge(REPO_NODE_ID, pkg_id, EdgeKind.CONTAINS)

        for rel_path in pkg_files[pkg_id]:
            abs_path = os.path.join(repo_path, rel_path)
            try:
                with open(abs_path, "rb") as f:
                    source = f.read()
            except OSError:
                continue
            result = extract_python_file(
                parser, sym_query, imp_query, source, rel_path, pkg_id
            )
            gb.add_node(result.file_node)
            gb.add_edge(pkg_id, result.file_node.id, EdgeKind.CONTAINS)
            for sym in result.symbols:
                gb.add_node(sym)
                gb.add_edge(result.file_node.id, sym.id, EdgeKind.CONTAINS)
            pkg_imports.setdefault(pkg_id, []).extend(result.raw_imports)

    for pkg_id, raw_imports in pkg_imports.items():
        seen: Set[str] = set()
        for raw in raw_imports:
            target = resolve_python_import(raw, pkg_ids)
            if not target or target == pkg_id or target in seen:
                continue
            seen.add(target)
            gb.add_edge(pkg_id, target, EdgeKind.IMPORT)

    return pkg_ids


def discover_python_packages(repo_path: str) -> Dict[str, List[str]]:
    """{pkgID (dir relpath): [file relpath, ...]} for all .py files."""
    pkgs: Dict[str, List[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [
            d for d in dirnames if d not in PY_DIR_SKIP and not d.startswith(".")
        ]
        for fn in filenames:
            if not fn.lower().endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, repo_path).replace(os.sep, "/")
            pkg_id = os.path.dirname(rel).replace(os.sep, "/") or "."
            pkgs.setdefault(pkg_id, []).append(rel)
    return pkgs


def extract_python_file(
    parser, sym_query, imp_query, source: bytes, rel_path: str, pkg_id: str
) -> FileResult:
    tree = tsparse.parse(parser, source)
    root = tree.root_node

    res = FileResult(
        file_node=GNode(
            id=rel_path,
            kind=NodeKind.FILE,
            name=os.path.basename(rel_path),
            file=rel_path,
            package=pkg_id,
        )
    )

    seen: Set[int] = set()
    for cap_name, name_node in tsparse.run_query(sym_query, root):
        start_byte = name_node.start_byte
        if start_byte in seen:
            continue
        seen.add(start_byte)

        symbol_name = tsparse.node_text(name_node)
        decl = _py_decl_node_for(name_node)

        kind = NodeKind.FUNCTION
        display_name = symbol_name
        if cap_name == "class":
            kind = NodeKind.STRUCT
        elif cap_name == "fn":
            cls = _py_enclosing_class_name(name_node)
            if cls:
                kind = NodeKind.METHOD
                display_name = f"{cls}.{symbol_name}"

        res.symbols.append(
            GNode(
                id=f"{pkg_id}/{display_name}",
                kind=kind,
                name=display_name,
                signature=_py_extract_signature(decl, source),
                doc=_py_extract_docstring(decl, source),
                file=rel_path,
                package=pkg_id,
                line=tsparse.node_start_line(name_node),
                end_line=tsparse.node_end_line(decl),
            )
        )

    for _cap_name, imp_node in tsparse.run_query(imp_query, root):
        raw = tsparse.node_text(imp_node).replace(".", "/")
        if raw:
            res.raw_imports.append(raw)

    return res


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _py_decl_node_for(name_node):
    n = name_node.parent
    while n is not None:
        if n.type in _DECL_KINDS:
            return n
        n = n.parent
    return name_node


def _py_enclosing_class_name(name_node) -> str:
    """Walk up from a function name node to an enclosing class_definition name."""
    n = name_node.parent
    while n is not None:
        if n.type in ("function_definition", "decorated_definition"):
            n = n.parent
            continue
        if n.type == "block":
            p = n.parent
            if p is not None and p.type == "class_definition":
                name_child = p.child_by_field_name("name")
                if name_child is not None:
                    return tsparse.node_text(name_child)
            return ""
        return ""
    return ""


def _py_inner_def(decl):
    if decl.type == "decorated_definition":
        inner = decl.child_by_field_name("definition")
        if inner is not None:
            return inner
    return decl


def _py_extract_signature(decl, source: bytes) -> str:
    inner = _py_inner_def(decl)
    if inner.type in _BODY_KINDS:
        for child in inner.children:
            if child.type == "block":
                text = source[inner.start_byte : child.start_byte]
                return text.decode("utf-8", errors="replace").strip()
    full = source[decl.start_byte : decl.end_byte].decode("utf-8", errors="replace").strip()
    if len(full) > 2000:
        full = full[:2000] + "…"
    return full


def _py_extract_docstring(decl, source: bytes) -> str:
    inner = _py_inner_def(decl)
    body = inner.child_by_field_name("body")
    if body is None:
        return ""
    for child in body.children:
        if not child.is_named:
            continue
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    text = source[sub.start_byte : sub.end_byte].decode(
                        "utf-8", errors="replace"
                    ).strip()
                    text = text.strip("\"'").strip("`")
                    return text.strip()
        break  # only the first statement can be a docstring
    return ""


def resolve_python_import(raw: str, pkg_ids: Set[str]) -> str:
    if not raw:
        return ""
    if raw in pkg_ids:
        return raw
    idx = raw.rfind("/")
    if idx > 0:
        parent = raw[:idx]
        if parent in pkg_ids:
            return parent
    return ""
