from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.agents.llm import as_text
from reviewer.agents.prompts import FINDINGS_TOOL
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import FileContext, Finding, PRContext, Severity
from reviewer.output.github_review import (
    FOOTER,
    bot_commented_lines,
    format_comment,
    suggestion_block,
)
from reviewer.pipeline import review
from reviewer.ranking import place

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"


def mk(**kw) -> Finding:
    base = {"agent": "correctness", "file": "app/pagination.py", "line": 3, "severity": Severity.HIGH,
            "category": "off-by-one", "title": "page is 1-indexed", "detail": "page=1 skips the first page",
            "confidence": 0.9}
    return Finding(**{**base, **kw})


# ---------------------------------------------------------------- schema & model


def test_findings_tool_schema_has_optional_replacement():
    item = FINDINGS_TOOL["input_schema"]["properties"]["findings"]["items"]
    assert item["properties"]["replacement"]["type"] == "string"
    assert "replacement" not in item["required"]


def test_replacement_normalised():
    assert mk(replacement="    start = (page - 1) * per_page\n").replacement == "    start = (page - 1) * per_page"
    assert mk(replacement="").replacement is None  # never a silent line deletion
    assert mk(replacement="   \n").replacement is None
    assert mk().replacement is None


def test_single_line_replacement_only():
    assert mk(replacement="x = 1").single_line_replacement == "x = 1"
    assert mk(replacement="x = 1\ny = 2").single_line_replacement is None


# ---------------------------------------------------------------- rendering


def test_comment_renders_suggestion_block_instead_of_prose():
    body = format_comment(mk(suggestion="subtract one before scaling", replacement="    start = (page - 1) * per_page"))
    assert "```suggestion\n    start = (page - 1) * per_page\n```" in body
    assert "**Suggested change:** subtract one before scaling" in body
    assert "Suggested fix" not in body
    assert FOOTER in body  # still recognisable as ours for incremental re-review


def test_suggestion_block_without_prose():
    body = format_comment(mk(replacement="    start = (page - 1) * per_page"))
    assert "**Suggested change:**\n\n```suggestion\n" in body


def test_multiline_replacement_falls_back_to_prose():
    body = format_comment(mk(suggestion="use (page - 1)", replacement="a = 1\nb = 2"))
    assert "```suggestion" not in body
    assert "**Suggested fix:** use (page - 1)" in body


def test_prose_only_unchanged():
    body = format_comment(mk(suggestion="use (page - 1)"))
    assert "**Suggested fix:** use (page - 1)" in body and "```" not in body


def test_fence_outgrows_backticks_in_replacement():
    block = suggestion_block('doc = "```python"')
    assert block.startswith("````suggestion\n") and block.endswith("\n````")


def test_bot_still_recognises_comments_with_suggestions():
    body = format_comment(mk(replacement="x"))
    assert bot_commented_lines([{"path": "app/pagination.py", "line": 3, "body": body}]) == {("app/pagination.py", 3)}


# ---------------------------------------------------------------- placement


def test_snapped_finding_loses_replacement():
    diff = parse_unified_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x\n+y\n z\n")[0]
    files = {"a.py": FileContext(diff=diff)}
    exact, near = mk(file="a.py", line=2, replacement="y2"), mk(file="a.py", line=5, replacement="w2")
    inline, _ = place([exact, near], files)
    assert [(f.line, f.replacement) for f in inline] == [(2, "y2"), (3, None)]


# ---------------------------------------------------------------- end to end


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


async def test_replacement_flows_through_pipeline_and_verifier_sees_it(settings):
    raw = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "off-by-one",
           "title": "page is 1-indexed", "detail": "page=1 skips the first page", "confidence": 0.9,
           "replacement": "    start = (page - 1) * per_page"}
    llm = FakeLLM({"correctness": [raw]})
    result = await review(await _pr(settings), llm, settings)
    assert [f.replacement for f in result.inline] == ["    start = (page - 1) * per_page"]

    verifier_user = as_text(llm.users[llm.calls.index("report_verdicts")])
    listing = json.loads(verifier_user.split("=== FINDINGS TO VERIFY ===\n", 1)[1])
    assert listing[0]["replacement"] == "    start = (page - 1) * per_page"

    assert "```suggestion\n    start = (page - 1) * per_page\n```" in format_comment(result.inline[0])


async def test_verifier_listing_omits_absent_replacement(settings):
    raw = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "off-by-one",
           "title": "t", "detail": "d", "confidence": 0.9}
    llm = FakeLLM({"correctness": [raw]})
    await review(await _pr(settings), llm, settings)
    verifier_user = as_text(llm.users[llm.calls.index("report_verdicts")])
    listing = json.loads(verifier_user.split("=== FINDINGS TO VERIFY ===\n", 1)[1])
    assert "replacement" not in listing[0]
