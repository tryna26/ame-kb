"""Offline tests: schema validation, node_no dedupe, JSON parsing. No DB/LLM."""
from ame_kb.extract import _extract_json, validate
from ame_kb.schema import edge_endpoints_ok, schema_prompt_block
from ame_kb.store import node_no, slug


def test_slug_and_node_no_dedupe():
    assert node_no("Person", "Ada Lovelace") == node_no("Person", "  ada   lovelace ")
    assert slug("Ada Lovelace") == "ada-lovelace"
    assert node_no("Person", "阿达") == "Person:阿达"


def test_edge_endpoints():
    assert edge_endpoints_ok("works_for", "Person", "Organization")
    assert not edge_endpoints_ok("works_for", "Organization", "Person")
    assert edge_endpoints_ok("related_to", "Concept", "Project")
    assert not edge_endpoints_ok("no_such_label", "Person", "Person")


def test_schema_prompt_block_lists_types():
    block = schema_prompt_block()
    assert "Person" in block and "works_for" in block


def test_validate_drops_bad_types_and_endpoints():
    payload = {
        "nodes": [
            {"name": "Ada", "type": "Person", "properties": {"title": "x", "bogus": 1}, "source": ["1-1"]},
            {"name": "ACME", "type": "Organization", "source": ["2-2"]},
            {"name": "Ghost", "type": "Alien", "source": ["3-3"]},
        ],
        "edges": [
            {"source_name": "Ada", "target_name": "ACME", "label": "works_for", "source": ["4-4"]},
            {"source_name": "ACME", "target_name": "Ada", "label": "works_for"},
            {"source_name": "Ada", "target_name": "Nobody", "label": "related_to"},
        ],
    }
    res = validate("doc1", payload)
    names = {n.name for n in res.nodes}
    assert names == {"Ada", "ACME"}  # Alien dropped
    ada = next(n for n in res.nodes if n.name == "Ada")
    assert ada.properties == {"title": "x", "bogus": 1}  # V2: extra field kept
    assert len(res.edges) == 1  # only Ada->ACME works_for survives
    assert res.edges[0].label == "works_for"
    assert len(res.dropped) == 3


def test_extract_json_tolerates_fences():
    raw = 'here you go:\n```json\n{"nodes": [], "edges": []}\n```\nthanks'
    assert _extract_json(raw) == {"nodes": [], "edges": []}
