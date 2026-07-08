"""Offline V3 tests: RRF, candidate pool, cosine, searchable_text, Ref parsing.
No DB/LLM/embedding calls."""
from ame_kb.recall import (
    _collect_line_numbers,
    _parse_line_range,
    build_candidate_pool,
    cosine,
)
from ame_kb.rrf import rrf_merge
from ame_kb.searchindex import build_searchable_text


def test_rrf_formula_single_list():
    # 1-based rank: 1/(60+1), 1/(60+2), ...
    merged = rrf_merge([["a", "b", "c"]])
    assert merged == ["a", "b", "c"]


def test_rrf_fuses_and_reorders():
    # b appears near the top of both lists -> should win over a and c.
    text_list = ["a", "b", "c"]
    vec_list = ["b", "c", "a"]
    merged = rrf_merge([text_list, vec_list])
    # b: 1/61 + 1/61 ; a: 1/61 + 1/63 ; c: 1/63 + 1/62
    assert merged[0] == "b"
    assert set(merged) == {"a", "b", "c"}


def test_rrf_score_value():
    # exact score check: only one list, single item at rank 1 -> 1/61
    from ame_kb.rrf import RRF_K

    assert RRF_K == 60
    # reproduce Σ 1/(60+rank)
    lists = [["x", "y"]]
    merged = rrf_merge(lists)
    assert merged == ["x", "y"]


def test_candidate_pool_union_dedup_order():
    node_hits = ["n1", "n2"]
    edge_hits = ["e1", "e2"]
    endpoints = {
        "e1": ("n2", "n3"),  # n2 dup (kept once), n3 new
        "e2": ("n4", "n1"),  # n4 new, n1 dup
    }
    pool = build_candidate_pool(node_hits, edge_hits, endpoints)
    assert pool == ["n1", "n2", "n3", "n4"]


def test_candidate_pool_skips_unknown_edges():
    pool = build_candidate_pool(["n1"], ["missing"], {})
    assert pool == ["n1"]


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert abs(cosine([1, 1], [1, 1]) - 1.0) < 1e-9
    assert cosine([], [1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0


def test_build_searchable_text_concat_and_skip():
    text = build_searchable_text(
        "Grace Hopper",
        "Project Aurora 负责人",
        {"title": "Chief Architect", "age": 40, "office": "Seattle"},
    )
    assert "Grace Hopper" in text
    assert "Project Aurora 负责人" in text
    assert "title: Chief Architect" in text
    assert "office: Seattle" in text
    # non-string value skipped
    assert "age" not in text


def test_build_searchable_text_skips_empty():
    assert build_searchable_text("", None, {}) == ""
    assert build_searchable_text("Name", "", {"k": "  "}) == "Name"


def test_parse_line_range():
    assert _parse_line_range("3") == [3]
    assert _parse_line_range("5-7") == [5, 6, 7]
    assert _parse_line_range("7-5") == [5, 6, 7]  # tolerates reversed
    assert _parse_line_range("") == []
    assert _parse_line_range("abc") == []


def test_collect_line_numbers_expands_and_merges():
    ref = {"aurora_memo.md": ["3", "5-7"]}
    by_doc = _collect_line_numbers(ref, window=0)
    # 3, then 5,6,7 ; gap 3->5 is 2 (<=3) so line 4 is filled in
    assert by_doc["aurora_memo.md"] == [3, 4, 5, 6, 7]


def test_collect_line_numbers_window():
    ref = {"d": ["5"]}
    by_doc = _collect_line_numbers(ref, window=1)
    assert by_doc["d"] == [4, 5, 6]


def test_collect_line_numbers_single_doc_multiple_tokens():
    ref = {"d": [10, "12"]}  # tokens can be ints or strings
    by_doc = _collect_line_numbers(ref, window=0)
    # gap 10->12 is 2 (<=3) -> 11 filled
    assert by_doc["d"] == [10, 11, 12]
