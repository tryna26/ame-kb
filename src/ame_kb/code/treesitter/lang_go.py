"""Go extractor (tree-sitter). Ports code2skill analyzer/treesitter/lang_go.go.

Captures function/method declarations and type declarations (struct/interface/
alias). Imports are resolved against the go.mod module prefix to in-repo package
IDs. Test files (_test.go) and vendor/testdata are skipped.
"""
from __future__ import annotations

import os
from typing import Dict, List, Set

from ..codegraph import EdgeKind, GNode, NodeKind
from .. import tsparse
from .builder import REPO_NODE_ID, FileResult, GraphBuilder

GO_SYMBOL_QUERY = """
(function_declaration name: (identifier) @fn)
(method_declaration name: (field_identifier) @method)
(type_declaration (type_spec name: (type_identifier) @type))
"""

GO_IMPORT_QUERY = """
(import_spec path: (interpreted_string_literal) @import)
"""

GO_DIR_SKIP = {".git", "vendor", "testdata"}

_BODY_DECL_KINDS = {"function_declaration", "method_declaration"}
_DECL_KINDS = {
    "function_declaration",
    "method_declaration",
    "type_declaration",
    "type_spec",
}


def build_go(repo_path: str, gb: GraphBuilder) -> Set[str]:
    lang = tsparse.get_language("go")
    parser = tsparse.new_parser(lang)
    sym_query = tsparse.compile_query(lang, GO_SYMBOL_QUERY)
    imp_query = tsparse.compile_query(lang, GO_IMPORT_QUERY)

    module_prefix = go_read_module_prefix(repo_path)

    pkg_files = discover_go_packages(repo_path)
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
            result = extract_go_file(
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
            target = resolve_go_import(raw, module_prefix, pkg_ids)
            if not target or target == pkg_id or target in seen:
                continue
            seen.add(target)
            gb.add_edge(pkg_id, target, EdgeKind.IMPORT)

    return pkg_ids


def discover_go_packages(repo_path: str) -> Dict[str, List[str]]:
    """{pkgID (dir relpath): [.go relpath, ...]}, skipping _test.go files."""
    pkgs: Dict[str, List[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [
            d for d in dirnames if d not in GO_DIR_SKIP and not d.startswith(".")
        ]
        for fn in filenames:
            if not fn.endswith(".go") or fn.endswith("_test.go"):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, repo_path).replace(os.sep, "/")
            pkg_id = os.path.dirname(rel).replace(os.sep, "/") or "."
            pkgs.setdefault(pkg_id, []).append(rel)
    return pkgs


def go_read_module_prefix(repo_path: str) -> str:
    path = os.path.join(repo_path, "go.mod")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line[len("module ") :].strip()
    except OSError:
        return ""
    return ""


def resolve_go_import(raw: str, module_prefix: str, pkg_ids: Set[str]) -> str:
    if not module_prefix:
        return raw if raw in pkg_ids else ""
    if not raw.startswith(module_prefix):
        return ""
    rel = raw[len(module_prefix) :].lstrip("/")
    if rel == "":
        rel = "."
    return rel if rel in pkg_ids else ""


def extract_go_file(
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
        decl = _go_declaration_for(name_node)
        kind = _go_symbol_kind(cap_name, decl)

        display_name = symbol_name
        if cap_name == "method":
            recv = _go_receiver_type_name(decl, source)
            if recv:
                display_name = f"{recv}.{symbol_name}"

        res.symbols.append(
            GNode(
                id=f"{pkg_id}/{display_name}",
                kind=kind,
                name=display_name,
                signature=_go_extract_signature(decl, source),
                doc=_go_extract_doc_comment(decl, source),
                file=rel_path,
                package=pkg_id,
                line=tsparse.node_start_line(name_node),
                end_line=tsparse.node_end_line(decl),
            )
        )

    for _cap_name, imp_node in tsparse.run_query(imp_query, root):
        raw = tsparse.node_text(imp_node).strip('"')
        if raw:
            res.raw_imports.append(raw)

    return res


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _go_declaration_for(name_node):
    n = name_node.parent
    while n is not None:
        if n.type in _DECL_KINDS:
            return n
        n = n.parent
    return name_node


def _go_symbol_kind(cap_name: str, decl) -> NodeKind:
    if cap_name == "fn":
        return NodeKind.FUNCTION
    if cap_name == "method":
        return NodeKind.METHOD
    # cap_name == "type": inspect the type_spec's underlying type.
    spec = decl
    if spec.type == "type_declaration":
        for child in spec.children:
            if child.type == "type_spec":
                spec = child
                break
    type_child = spec.child_by_field_name("type")
    if type_child is not None:
        if type_child.type == "struct_type":
            return NodeKind.STRUCT
        if type_child.type == "interface_type":
            return NodeKind.INTERFACE
    return NodeKind.TYPE_ALIAS


def _go_extract_signature(decl, source: bytes) -> str:
    if decl.type in _BODY_DECL_KINDS:
        for child in decl.children:
            if child.type == "block":
                return source[decl.start_byte : child.start_byte].decode(
                    "utf-8", errors="replace"
                ).strip()
    full = source[decl.start_byte : decl.end_byte].decode(
        "utf-8", errors="replace"
    ).strip()
    if len(full) > 2000:
        full = full[:2000] + "…"
    return full


def _go_extract_doc_comment(decl, source: bytes) -> str:
    prev = decl.prev_named_sibling
    if prev is not None and prev.type == "comment":
        return source[prev.start_byte : prev.end_byte].decode(
            "utf-8", errors="replace"
        ).strip()
    return ""


def _go_receiver_type_name(decl, source: bytes) -> str:
    if decl.type != "method_declaration":
        return ""
    recv = decl.child_by_field_name("receiver")
    if recv is None:
        return ""
    for param in recv.children:
        if param.type != "parameter_declaration":
            continue
        type_node = param.child_by_field_name("type")
        if type_node is None:
            continue
        return tsparse.node_text(type_node).lstrip("*")
    return ""
