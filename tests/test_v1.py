"""Offline tests: schema validation, node_no dedupe, JSON parsing. No DB/LLM."""
from ame_kb.extract import _extract_json, validate
from ame_kb.schema import schema_prompt_block
from ame_kb.store import node_no, slug


def test_slug_and_node_no_dedupe():
    # Asset node_no carries the archetype; same type+spec+name collide.
    assert node_no("Asset", "Solution", "Ada Lovelace") == node_no(
        "Asset", "Solution", "  ada   lovelace "
    )
    assert node_no("Asset", "Solution", "AdStyle") == "Asset:Solution:adstyle"
    # Non-Asset nodes have no spec segment.
    assert node_no("Event", None, "阿达") == "Event:阿达"
    # Same name, different archetype -> distinct nodes (layered ontology).
    assert node_no("Asset", "Mission", "Login") != node_no(
        "Asset", "Implementation", "Login"
    )


def test_schema_prompt_block_lists_types():
    block = schema_prompt_block()
    assert "Asset" in block and "Relation" in block
    assert "Mission" in block and "Solution" in block


def test_validate_drops_bad_types_and_endpoints():
    payload = {
        "nodes": [
            {"name": "AdStyle", "entity_type": "Asset", "entity_spec": "Solution",
             "description": "方案", "properties": {"owner": "x", "bogus": 1}, "source": ["1-1"]},
            {"name": "Experiment", "entity_type": "Event", "source": ["2-2"]},
            {"name": "Ghost", "entity_type": "Alien", "source": ["3-3"]},
            {"name": "NoSpec", "entity_type": "Asset", "source": ["4-4"]},
        ],
        "edges": [
            {"source_name": "AdStyle", "target_name": "Experiment", "label": "used_by",
             "confidence": "EXTRACTED", "source": ["5-5"]},
            {"source_name": "AdStyle", "target_name": "Nobody", "label": "related_to"},
            {"source_name": "AdStyle", "target_name": "Experiment", "label": "x",
             "confidence": "BOGUS"},
        ],
    }
    res = validate("doc1", payload)
    names = {n.name for n in res.nodes}
    assert names == {"AdStyle", "Experiment"}  # Alien + Asset-without-spec dropped
    ad = next(n for n in res.nodes if n.name == "AdStyle")
    assert ad.entity_type == "Asset" and ad.entity_spec == "Solution"
    assert ad.properties == {"owner": "x", "bogus": 1}  # extra field kept
    assert ad.description == "方案"
    ev = next(n for n in res.nodes if n.name == "Experiment")
    assert ev.entity_type == "Event" and ev.entity_spec is None
    assert len(res.edges) == 1  # only AdStyle->Experiment used_by survives
    assert res.edges[0].label == "used_by"
    assert len(res.dropped) == 4


def test_extract_json_tolerates_fences():
    raw = 'here you go:\n```json\n{"nodes": [], "edges": []}\n```\nthanks'
    assert _extract_json(raw) == {"nodes": [], "edges": []}
