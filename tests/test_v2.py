"""Offline V2 tests: source dispatch, content hash, semi-dynamic props, edge
confidence. No DB/LLM."""
from pathlib import Path

import pytest

from ame_kb import sources
from ame_kb.extract import DEFAULT_CONFIDENCE, validate
from ame_kb.store import content_hash


def test_sources_supported_suffixes():
    assert sources.is_supported(Path("a.md"))
    assert sources.is_supported(Path("a.pdf"))
    assert sources.is_supported(Path("a.HTML"))  # case-insensitive
    assert not sources.is_supported(Path("a.docx"))


def test_sources_load_text(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world", encoding="utf-8")
    assert sources.load(p) == "hello world"


def test_sources_load_unsupported_raises(tmp_path):
    p = tmp_path / "x.docx"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        sources.load(p)


def test_content_hash_is_stable_and_sensitive():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 64


def test_validate_keeps_extra_properties():
    payload = {
        "nodes": [
            {
                "name": "Ada",
                "type": "Person",
                "properties": {"title": "x", "birth_year": 1815},
                "source": ["1-1"],
            }
        ],
        "edges": [],
    }
    res = validate("doc1", payload)
    ada = res.nodes[0]
    assert ada.properties == {"title": "x", "birth_year": 1815}
    assert ada.description == ""  # V3: description defaults to empty when absent


def test_validate_edge_confidence_default_and_gate():
    payload = {
        "nodes": [
            {"name": "Ada", "type": "Person", "source": ["1-1"]},
            {"name": "ACME", "type": "Organization", "source": ["2-2"]},
            {"name": "Doc", "type": "Document", "source": ["3-3"]},
        ],
        "edges": [
            # no confidence -> default INFERRED, kept
            {"source_name": "Ada", "target_name": "ACME", "label": "works_for"},
            # explicit EXTRACTED, kept
            {
                "source_name": "Ada",
                "target_name": "Doc",
                "label": "authored",
                "confidence": "EXTRACTED",
            },
            # bad confidence -> dropped
            {
                "source_name": "Ada",
                "target_name": "ACME",
                "label": "related_to",
                "confidence": "MAYBE",
            },
        ],
    }
    res = validate("doc1", payload)
    labels = {(e.label, e.confidence) for e in res.edges}
    assert ("works_for", DEFAULT_CONFIDENCE) in labels
    assert ("authored", "EXTRACTED") in labels
    assert len(res.edges) == 2
    assert any("bad confidence" in d for d in res.dropped)
