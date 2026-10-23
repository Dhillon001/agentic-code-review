from pathlib import Path

from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext
from reviewer.pipeline import review

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


REAL = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "off-by-one",
        "title": "page is 1-indexed", "detail": "page=1 skips the first page", "confidence": 0.9}
BOGUS = {"file": "app/pagination.py", "line": 5, "severity": "medium", "category": "perf",
         "title": "slicing copies", "detail": "meh", "confidence": 0.7}


async def test_end_to_end_verifier_kills_false_positive(settings):
    llm = FakeLLM({"correctness": [REAL], "performance": [BOGUS]}, reject={("app/pagination.py", 5)})
    result = await review(await _pr(settings), llm, settings)
    assert [(f.file, f.line) for f in result.inline] == [("app/pagination.py", 3)]
    assert result.inline[0].verified
    assert [f.line for f in result.rejected] == [5]
    assert result.stats["agent_calls"] == 4


async def test_low_confidence_dropped_before_verify(settings):
    llm = FakeLLM({"correctness": [{**REAL, "confidence": 0.2}]})
    result = await review(await _pr(settings), llm, settings)
    assert result.inline == [] and "report_verdicts" not in llm.calls


async def test_hallucinated_file_dropped(settings):
    llm = FakeLLM({"security": [{**REAL, "file": "nope.py"}]})
    result = await review(await _pr(settings), llm, settings)
    assert result.stats["raw_findings"] == 0
