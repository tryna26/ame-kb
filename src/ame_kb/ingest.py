"""Ingest: scan a folder for supported sources and prepare line-numbered documents.

Source formats (md/txt/pdf/html) are normalized to plain text by `sources.load`
before extraction (oceanai's "normalize every source into a doc" idea).

The line-number prefix ([N] ...) is borrowed from oceanai's addLineNumbers:
it lets the LLM cite source line ranges when it extracts entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

from . import sources


@dataclass
class Document:
    doc_id: str  # relative path from the scanned root, used as the source key
    path: Path
    text: str

    def numbered_text(self) -> str:
        return add_line_numbers(self.text)


def add_line_numbers(text: str) -> str:
    """Prefix each line with [N] (1-based), mirroring oceanai's approach."""
    lines = text.splitlines()
    return "\n".join(f"[{i}] {line}" for i, line in enumerate(lines, start=1))


def scan(source_dir: str) -> List[Document]:
    return list(iter_documents(source_dir))


def iter_documents(source_dir: str) -> Iterator[Document]:
    root = Path(source_dir).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"SOURCE_DIR does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not sources.is_supported(path):
            continue
        text = sources.load(path)
        if not text.strip():
            continue
        doc_id = str(path.relative_to(root))
        yield Document(doc_id=doc_id, path=path, text=text)
