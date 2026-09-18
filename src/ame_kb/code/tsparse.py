"""py-tree-sitter adapter: isolate the binding API in one place.

The query/match API shifted across py-tree-sitter releases (0.23 exposes
Query.matches(node); 0.24+ moved it to QueryCursor(query).matches(node)). All
call sites go through run_query() so the language extractors never touch the
version-specific surface. Import errors surface a friendly hint pointing at the
optional `code` extra.
"""
from __future__ import annotations

from typing import List, Tuple

try:
    import tree_sitter as _ts
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "Code extraction needs tree-sitter. Install the optional extra:\n"
        "    pip install 'ame-kb[code]'"
    ) from exc


_LANG_LOADERS = {
    "python": ("tree_sitter_python", "language"),
    "go": ("tree_sitter_go", "language"),
}


def get_language(lang: str) -> "_ts.Language":
    spec = _LANG_LOADERS.get(lang)
    if spec is None:
        raise ValueError(
            f"unsupported language {lang!r} — supported: {', '.join(sorted(_LANG_LOADERS))}"
        )
    module_name, attr = spec
    try:
        module = __import__(module_name)
    except ImportError as exc:
        raise ImportError(
            f"Missing grammar for {lang!r}. Install the optional extra:\n"
            "    pip install 'ame-kb[code]'"
        ) from exc
    return _ts.Language(getattr(module, attr)())


def new_parser(language: "_ts.Language") -> "_ts.Parser":
    return _ts.Parser(language)


def compile_query(language: "_ts.Language", source: str) -> "_ts.Query":
    return _ts.Query(language, source)


def parse(parser: "_ts.Parser", source: bytes) -> "_ts.Tree":
    return parser.parse(source)


def run_query(query: "_ts.Query", root: "_ts.Node") -> List[Tuple[str, "_ts.Node"]]:
    """Return [(capture_name, node), ...] flattened across all matches.

    Absorbs the 0.23 Query.matches vs 0.24 QueryCursor.matches difference.
    """
    if hasattr(_ts, "QueryCursor"):  # 0.24+
        cursor = _ts.QueryCursor(query)
        raw = cursor.matches(root)
    else:  # 0.23
        raw = query.matches(root)

    out: List[Tuple[str, "_ts.Node"]] = []
    for _pattern_idx, caps in raw:
        for capture_name, nodes in caps.items():
            for node in nodes:
                out.append((capture_name, node))
    return out


def node_text(node: "_ts.Node") -> str:
    return node.text.decode("utf-8", errors="replace")


def node_start_line(node: "_ts.Node") -> int:
    """1-based start line."""
    return int(node.start_point.row) + 1


def node_end_line(node: "_ts.Node") -> int:
    """1-based end line."""
    return int(node.end_point.row) + 1
