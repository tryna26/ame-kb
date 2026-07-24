"""Source classification + prompt routing (multi-source adaptive extraction).

Covers srcclass.classify_source bucketing (code suffix / plan keywords /
generic fallback) and that extract.extract threads the right prompt template to
the LLM call per source kind.
"""
from __future__ import annotations

from pathlib import Path

from ame_kb.ingest import Document
from ame_kb.srcclass import SourceKind, classify_source


def _doc(doc_id: str, text: str = "hello world") -> Document:
    return Document(doc_id=doc_id, path=Path(doc_id), text=text)


# ---- classify_source bucketing (pure) ----

def test_classify_code_by_suffix():
    assert classify_source(_doc("src/app.py")) is SourceKind.CODE
    assert classify_source(_doc("pkg/main.go")) is SourceKind.CODE
    assert classify_source(_doc("web/app.tsx")) is SourceKind.CODE


def test_classify_plan_by_path_keyword():
    assert classify_source(_doc("docs/roadmap.md")) is SourceKind.PLAN
    assert classify_source(_doc("PLAN.md")) is SourceKind.PLAN
    assert classify_source(_doc("产品需求.md")) is SourceKind.PLAN


def test_classify_plan_by_title_keyword():
    # doc_id has no keyword, but the first heading (title) does.
    doc = _doc("notes/n1.md", text="# 项目计划书\n\n正文")
    assert classify_source(doc) is SourceKind.PLAN


def test_classify_generic_fallback():
    assert classify_source(_doc("notes/meeting.md")) is SourceKind.DOC
    assert classify_source(_doc("readme.txt", text="just some text")) is SourceKind.DOC


def test_code_suffix_takes_priority_over_plan_keyword():
    # A code file whose name contains a plan keyword still routes to CODE.
    assert classify_source(_doc("plan_engine.py")) is SourceKind.CODE


# ---- prompt routing in extract() ----

def _capture_template(monkeypatch):
    import ame_kb.extract as ex

    seen = {"prompt": ""}

    def fake_call_llm(prompt):
        seen["prompt"] = prompt
        return '{"nodes": [], "edges": []}'

    monkeypatch.setattr(ex, "call_llm", fake_call_llm)
    monkeypatch.setattr(ex, "schema_prompt_block", lambda: "SCHEMA")
    return seen


def test_extract_plan_uses_plan_prompt(monkeypatch):
    import ame_kb.extract as ex

    seen = _capture_template(monkeypatch)
    ex.extract(_doc("docs/roadmap.md", text="# 路线图\n目标"))
    # The plan prompt is the only variant that mentions 里程碑/交付物 language.
    assert "项目计划书" in seen["prompt"]


def test_extract_doc_uses_default_prompt(monkeypatch):
    import ame_kb.extract as ex

    seen = _capture_template(monkeypatch)
    ex.extract(_doc("notes/meeting.md", text="# 周会\n讨论"))
    # Default prompt does not carry the plan-specific header.
    assert "项目计划书" not in seen["prompt"]
    assert "知识图谱本体抽取器" in seen["prompt"]


def test_extract_code_falls_back_to_default_prompt(monkeypatch):
    # Until tree-sitter lands, CODE routes to the generic prompt (no crash).
    import ame_kb.extract as ex

    seen = _capture_template(monkeypatch)
    ex.extract(_doc("src/app.py", text="def f():\n    return 1"))
    assert "项目计划书" not in seen["prompt"]
    assert "知识图谱本体抽取器" in seen["prompt"]
