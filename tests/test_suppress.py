from pathlib import Path

from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import Finding, PRContext, Severity
from reviewer.pipeline import review
from reviewer.suppress import is_suppressed, parse_suppressions, split_suppressed


def mk(line: int, agent: str = "correctness", category: str = "off-by-one", file: str = "a.py") -> Finding:
    return Finding(agent=agent, file=file, line=line, severity=Severity.HIGH, category=category, title="t", detail="d")


# ---------------------------------------------------------------- parsing


def test_same_line_any_comment_syntax():
    src = "a = 1  # acr: ignore\nb = 2  // ACR: Ignore\nc = 3 /* acr:ignore */\nd = 4 -- nothing here\n"
    assert parse_suppressions(src) == {1: None, 2: None, 3: None}


def test_next_line_directive():
    src = "# acr: ignore-next-line\nrisky()\nfine()\n"
    assert parse_suppressions(src) == {2: None}


def test_scoped_tokens_lowercased_and_merged():
    src = "x()  # acr: ignore[Security, sql-injection]\n# acr: ignore-next-line[performance]\ny()  # acr: ignore[tests]\n"
    rules = parse_suppressions(src)
    assert rules[1] == {"security", "sql-injection"}
    assert rules[3] == {"performance", "tests"}  # next-line from 2 merged with same-line on 3


def test_unscoped_wins_over_scoped_on_same_line():
    src = "# acr: ignore-next-line[security]\nz()  # acr: ignore\n"
    assert parse_suppressions(src) == {2: None}
    src = "# acr: ignore-next-line\nz()  # acr: ignore[security]\n"
    assert parse_suppressions(src) == {2: None}


def test_empty_scope_means_everything():
    assert parse_suppressions("q()  # acr: ignore[]\n") == {1: None}


def test_no_content_or_no_directives():
    assert parse_suppressions(None) == {}
    assert parse_suppressions("") == {}
    assert parse_suppressions("ignore this\nacr ignore\n") == {}


# ---------------------------------------------------------------- matching


def test_scope_matches_agent_or_category_case_insensitively():
    rules = {5: frozenset({"security", "off-by-one"})}
    assert is_suppressed(mk(5, agent="Security", category="sql-injection"), rules)
    assert is_suppressed(mk(5, agent="correctness", category="Off-By-One"), rules)
    assert not is_suppressed(mk(5, agent="performance", category="n-plus-one"), rules)
    assert not is_suppressed(mk(6, agent="security"), rules)


def test_split_by_file():
    kept, dropped = split_suppressed([mk(1, file="a.py"), mk(1, file="b.py")], {"a.py": {1: None}})
    assert [f.file for f in kept] == ["b.py"] and [f.file for f in dropped] == ["a.py"]


# ---------------------------------------------------------------- pipeline

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"
PATH = "app/pagination.py"
REAL = {"file": PATH, "line": 3, "severity": "high", "category": "off-by-one",
        "title": "page is 1-indexed", "detail": "page=1 skips the first page", "confidence": 0.9}


async def _pr(settings, edit_line: int | None = None, comment: str = "") -> PRContext:
    _, diff, files = load_case(CASE)
    if edit_line is not None:
        lines = files[PATH].splitlines()
        lines[edit_line - 1] += f"  {comment}"
        files[PATH] = "\n".join(lines) + "\n"
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


async def test_suppressed_finding_dropped_before_verifier(settings):
    llm = FakeLLM({"correctness": [REAL]})
    result = await review(await _pr(settings, 3, "# acr: ignore"), llm, settings)
    assert result.inline == [] and result.summary_only == []
    assert result.stats["suppressed"] == 1
    assert "report_verdicts" not in llm.calls  # never paid to verify it


async def test_scope_for_other_agent_does_not_suppress(settings):
    llm = FakeLLM({"correctness": [REAL]})
    result = await review(await _pr(settings, 3, "# acr: ignore[security]"), llm, settings)
    assert [f.line for f in result.inline] == [3]
    assert result.stats["suppressed"] == 0


async def test_finding_snapped_onto_suppressed_line_is_dropped(settings):
    # Line 7 is off-diff; place() snaps it to the nearest diff line (5), which is suppressed.
    llm = FakeLLM({"correctness": [{**REAL, "line": 7}]})
    result = await review(await _pr(settings, 5, "# acr: ignore"), llm, settings)
    assert result.inline == [] and result.summary_only == []
    assert result.stats["suppressed"] == 1
