"""Ingest: scan a folder for supported sources and prepare line-numbered documents.

Source formats (md/txt/pdf/html) are normalized to plain text by `sources.load`
before extraction.

The line-number prefix ([N] ...) lets the LLM cite source line ranges when it
extracts entities.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

from sqlalchemy import delete, select

from . import sources
from .config import get_settings
from .db import session_scope
from .models import Doc, DocLine


@dataclass
class Document:
    doc_id: str  # relative path from the scanned root, used as the source key
    path: Path
    text: str
    origin_url: str = ""  # set for URL sources; empty for local files

    def numbered_text(self) -> str:
        return add_line_numbers(self.text)

    @property
    def source_type(self) -> str:
        if self.origin_url:
            return "url"
        return self.path.suffix.lower().lstrip(".")

    @property
    def title(self) -> str:
        for line in self.text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:512]
        return self.doc_id


def add_line_numbers(text: str) -> str:
    """Prefix each line with [N] (1-based) for source-range citations."""
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


def _url_doc_id(url: str) -> str:
    """Stable doc business key for a URL: 'url:<host><path>' truncated safely."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    key = f"{parsed.netloc}{parsed.path}".rstrip("/") or parsed.netloc or url
    doc_id = f"url:{key}"
    return doc_id[:512]


def load_url(url: str) -> Document:
    """Fetch a URL into a Document (origin_url set for provenance)."""
    text = sources.fetch_url(url)
    if not text.strip():
        raise ValueError(f"URL produced no extractable text: {url}")
    return Document(
        doc_id=_url_doc_id(url),
        path=Path(url),
        text=text,
        origin_url=url,
    )


def read_url_list(list_path: str) -> List[str]:
    """Read a newline-delimited URL manifest, skipping blanks and # comments."""
    urls: List[str] = []
    for line in Path(list_path).expanduser().read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            urls.append(s)
    return urls


def _content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def persist_doc(doc: Document) -> None:
    """Upsert kg_doc (sha256 fingerprint + provenance), rewrite kg_doc_line, and
    (re)build the doc's chunks.

    Call only after a successful store so a failed extraction never poisons the
    incremental cache (llm_wiki pattern). The sha256 here is what is_unchanged
    checks on the next run, and what chunk rebuild keys its skip on.
    """
    settings = get_settings()
    new_hash = _content_hash(doc.text)
    with session_scope() as session:
        existing = session.execute(
            select(Doc).where(
                Doc.graph_no == settings.graph_no,
                Doc.graph_version == settings.graph_version,
                Doc.doc_no == doc.doc_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Doc(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    doc_no=doc.doc_id,
                    path=str(doc.path),
                    title=doc.title,
                    sha256=new_hash,
                    source_type=doc.source_type,
                    origin_url=doc.origin_url,
                )
            )
        else:
            existing.path = str(doc.path)
            existing.title = doc.title
            existing.sha256 = new_hash
            existing.source_type = doc.source_type
            existing.origin_url = doc.origin_url

        # Rewrite line rows so Ref -> original-text lookup always matches the
        # current numbering.
        session.execute(
            delete(DocLine).where(
                DocLine.graph_no == settings.graph_no,
                DocLine.graph_version == settings.graph_version,
                DocLine.doc_no == doc.doc_id,
            )
        )
        for i, line in enumerate(doc.text.splitlines(), start=1):
            session.add(
                DocLine(
                    graph_no=settings.graph_no,
                    graph_version=settings.graph_version,
                    doc_no=doc.doc_id,
                    line_no=i,
                    content=line,
                )
            )

    # Rebuild chunks (own transaction; skips when the doc hash is unchanged).
    from .docchunk import persist_chunks

    persist_chunks(
        doc.doc_id,
        doc.text,
        new_hash,
        origin_url=doc.origin_url,
        file_path=str(doc.path),
    )
