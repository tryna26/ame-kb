"""Source loaders: turn heterogeneous files into plain text.

Borrowed from oceanai's "normalize every source into a doc before extraction"
idea: each loader takes a path and returns plain text, so the downstream
Document / extraction path stays identical regardless of source format.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

# Text-native formats read directly; binary/markup formats go through a parser.
SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".html", ".htm"}


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _load_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _load_html(path: Path) -> str:
    import trafilatura

    raw = path.read_text(encoding="utf-8", errors="replace")
    extracted = trafilatura.extract(raw)
    return extracted or ""


_LOADERS: Dict[str, Callable[[Path], str]] = {
    ".md": _load_text,
    ".txt": _load_text,
    ".pdf": _load_pdf,
    ".html": _load_html,
    ".htm": _load_html,
}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def load(path: Path) -> str:
    """Return plain text for a supported source file."""
    loader = _LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported source type: {path.suffix} ({path})")
    return loader(path)


def fetch_url(url: str, timeout: float = 30.0) -> str:
    """Fetch a web page and return its main-content text.

    Uses trafilatura's own fetch (handles encoding/redirects) then main-content
    extraction, the same extractor used for local .html files, so URL and file
    sources normalize to identical downstream text.
    """
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ValueError(f"Failed to fetch URL: {url}")
    extracted = trafilatura.extract(downloaded)
    return extracted or ""
