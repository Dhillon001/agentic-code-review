import asyncio
import re
from types import SimpleNamespace

import pytest
from conftest import FakeLLM
from test_pipeline import CASE, REAL

from evals.run_evals import load_case
from reviewer.agents.llm import as_text
from reviewer.agents.resilience import (
    ResilientLLM,
    RetryPolicy,
    is_transient,
    retry_after_s,
)
from reviewer.cli import format_text
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext, ReviewResult
from reviewer.output.github_review import format_summary
from reviewer.output.markdown import format_markdown
from reviewer.pipeline import review

TOOL = {"name": "report_findings"}


class RateLimitError(Exception):  # same class name as the SDK's; is_transient matches by name
    pass


class APIStatusError(Exception):
    def __init__(self, status_code: int, headers: dict | None = None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class Scripted:
    """Raises the scripted exceptions in order, then succeeds."""

    def __init__(self, *errors: BaseException, hang: float = 0.0):
        self.errors = list(errors)
        self.hang = hang
        self.calls = 0

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.hang:
            await asyncio.sleep(self.hang)
        return {"findings": []}


def _resilient(inner, **policy):
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)

    policy = {"backoff_base_s": 1.0, "backoff_max_s": 30.0, **policy}
    return ResilientLLM(inner, RetryPolicy(**policy), sleep=sleep, rng=lambda: 1.0), sleeps


async def _call(llm):
    return await llm.call_tool(model="m", system="s", user="u", tool=TOOL)


# ------------------------------------------------------------------ classification + backoff


@pytest.mark.parametrize(
    "exc, transient",
    [
        (RateLimitError("slow down"), True),
        (APIStatusError(429), True),
        (APIStatusError(529), True),  # Anthropic "overloaded"
        (APIStatusError(503), True),
        (APIStatusError(400), False),
        (APIStatusError(401), False),
        (asyncio.TimeoutError(), True),
        (ConnectionResetError(), True),
        (RuntimeError("m did not call report_findings"), False),
        (ValueError("bad"), False),
    ],
)
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


def test_backoff_is_exponential_capped_and_jittered():
    p = RetryPolicy(backoff_base_s=1.0, backoff_max_s=5.0)
    assert [p.delay(n, lambda: 1.0) for n in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert p.delay(2, lambda: 0.25) == 1.0  # full jitter: uniform in [0, cap]
    assert p.delay(3, lambda: 0.0) == 0.0


def test_retry_after_header():
    assert retry_after_s(APIStatusError(429, {"retry-after": "7"})) == 7.0
    assert retry_after_s(APIStatusError(429, {"retry-after": "soon"})) is None
    assert retry_after_s(RateLimitError()) is None


# ------------------------------------------------------------------ ResilientLLM


async def test_retries_transient_errors_then_succeeds():
    inner = Scripted(RateLimitError(), APIStatusError(529), APIStatusError(503))
    llm, sleeps = _resilient(inner, max_retries=3)
    assert await _call(llm) == {"findings": []}
    assert inner.calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_gives_up_after_max_retries():
    inner = Scripted(*[APIStatusError(529) for _ in range(5)])
    llm, sleeps = _resilient(inner, max_retries=2)
    with pytest.raises(APIStatusError):
        await _call(llm)
    assert inner.calls == 3 and len(sleeps) == 2


async def test_permanent_errors_fail_fast():
    inner = Scripted(APIStatusError(400))
    llm, sleeps = _resilient(inner, max_retries=3)
    with pytest.raises(APIStatusError):
        await _call(llm)
    assert inner.calls == 1 and sleeps == []


async def test_retry_after_is_honoured_up_to_the_cap():
    inner = Scripted(APIStatusError(429, {"retry-after": "12"}), APIStatusError(429, {"retry-after": "999"}))
    llm, sleeps = _resilient(inner, max_retries=3, backoff_max_s=20.0)
    await _call(llm)
    assert sleeps == [12.0, 20.0]


async def test_per_attempt_timeout_is_retried():
    class SlowOnce:
        calls = 0

        async def call_tool(self, **kw):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(10)
            return {"findings": ["ok"]}

    inner = SlowOnce()
    llm, sleeps = _resilient(inner, timeout_s=0.01, max_retries=1)
    assert await _call(llm) == {"findings": ["ok"]}
    assert inner.calls == 2 and len(sleeps) == 1


async def test_concurrency_limit_is_held_only_while_calling():
    in_flight = peak = 0

    class Counting:
        async def call_tool(self, **kw):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1
            return {}

    llm = ResilientLLM(Counting(), RetryPolicy(), concurrency=2)
    await asyncio.gather(*(_call(llm) for _ in range(6)))
    assert peak == 2


def test_settings_build_policy(settings):
    s = settings.model_copy(update={"llm_timeout_s": 0, "llm_max_retries": 5, "llm_backoff_max_s": 8})
    p = s.retry_policy()
    assert (p.timeout_s, p.max_retries, p.backoff_max_s) == (None, 5, 8)
    assert settings.retry_policy().timeout_s == 180.0


# ------------------------------------------------------------------ pipeline: partial reviews


class FlakyLLM(FakeLLM):
    """FakeLLM where some specialists (and optionally the verifier) always fail."""

    def __init__(self, findings, failing: set[str], verifier_fails: bool = False):
        super().__init__(findings)
        self.failing = failing
        self.verifier_fails = verifier_fails

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        if tool["name"] == "report_findings":
            agent = re.search(r"You are the `([\w-]+)` reviewer", as_text(user)).group(1)
            if agent in self.failing:
                raise APIStatusError(529)
        elif self.verifier_fails:
            raise asyncio.TimeoutError()
        return await super().call_tool(model=model, system=system, user=user, tool=tool, max_tokens=max_tokens)


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


async def test_failed_specialist_gives_partial_review_not_crash(settings):
    llm = FlakyLLM({"correctness": [REAL]}, failing={"security", "tests"})
    result = await review(await _pr(settings), llm, settings)
    assert [(f.file, f.line) for f in result.inline] == [("app/pagination.py", 3)]
    assert result.failed_agents == ["security", "tests"]
    assert result.partial
    assert result.stats["agent_errors"] == 2


async def test_failed_verifier_holds_findings_back(settings):
    llm = FlakyLLM({"correctness": [REAL]}, failing=set(), verifier_fails=True)
    result = await review(await _pr(settings), llm, settings)
    assert result.inline == [] and result.summary_only == []
    assert [f.verifier_note for f in result.rejected] == ["verifier failed: TimeoutError"]
    assert result.failed_agents == ["verifier"]
    assert result.stats["verifier_errors"] == 1


async def test_everything_failing_still_returns_a_result(settings):
    llm = FlakyLLM({}, failing={"correctness", "security", "performance", "tests"})
    result = await review(await _pr(settings), llm, settings)
    assert result.failed_agents == ["correctness", "security", "performance", "tests"]
    assert result.stats["posted"] == 0


async def test_retries_through_resilient_wrapper_in_pipeline(settings):
    class FlakyOnce(FakeLLM):
        failed = False

        async def call_tool(self, **kw):
            if not self.failed:
                self.failed = True
                raise RateLimitError()
            return await super().call_tool(**kw)

    llm = ResilientLLM(FlakyOnce({"correctness": [REAL]}), RetryPolicy(backoff_base_s=0))
    result = await review(await _pr(settings), llm, settings)
    assert result.failed_agents == [] and len(result.inline) == 1


# ------------------------------------------------------------------ output


def test_partial_review_is_flagged_in_every_report():
    result = ReviewResult(failed_agents=["security", "verifier"])
    summary = format_summary(result)
    assert "Partial review" in summary and "`security`, `verifier`" in summary
    assert "Partial review" in format_markdown(result)
    assert "partial review: security, verifier" in format_text(result)
    assert "Partial review" not in format_summary(ReviewResult())
    assert "Partial review" not in format_markdown(ReviewResult())
    assert ReviewResult.model_validate_json(result.model_dump_json()).failed_agents == ["security", "verifier"]
