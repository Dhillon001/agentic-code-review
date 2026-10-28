from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.agents.llm import AnthropicLLM
from reviewer.cli import print_result
from reviewer.config import Settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext, ReviewResult
from reviewer.output.github_review import format_summary
from reviewer.pipeline import review
from reviewer.usage import (
    DEFAULT_PRICES,
    CallUsage,
    ModelPrice,
    Usage,
    format_usage,
    price_for,
    record_call,
    recording,
    summarize,
)

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"


class MeteredLLM(FakeLLM):
    """FakeLLM that reports usage the way AnthropicLLM does: 1000 in (600 of it cached) / 100 out."""

    async def call_tool(self, **kw):
        record_call(CallUsage(model=kw["model"], input_tokens=400, output_tokens=100, cache_read_tokens=600,
                              latency_s=0.5))
        return await super().call_tool(**kw)


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


# ------------------------------------------------------------------ pricing


def test_price_cost_uses_cache_multipliers():
    p = ModelPrice(input=10.0, output=20.0)
    call = CallUsage(model="m", input_tokens=1_000_000, output_tokens=1_000_000,
                     cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert p.cost(call) == pytest.approx(10 + 20 + 1 + 12.5)
    explicit = ModelPrice(input=10.0, output=20.0, cache_read=2.0, cache_write=0.0)
    assert explicit.cost(call) == pytest.approx(10 + 20 + 2)


def test_price_for_exact_then_longest_prefix():
    prices = {"claude-opus": ModelPrice(input=5, output=25), "claude-opus-5-5": ModelPrice(input=1, output=1),
              "claude": ModelPrice(input=9, output=9)}
    assert price_for("claude-opus-5-5", prices).input == 1
    assert price_for("claude-opus-4-8", prices).input == 5
    assert price_for("claude-sonnet-5-5", prices).input == 9
    assert price_for("gpt-x", prices) is None
    assert price_for("claude-sonnet-5-5", DEFAULT_PRICES) is not None


def test_model_prices_overridable_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_PRICES", json.dumps({"my-model": {"input": 2, "output": 4}}))
    s = Settings(_env_file=None)
    assert s.model_prices == {"my-model": ModelPrice(input=2, output=4)}


# ------------------------------------------------------------------ aggregation


def test_summarize_totals_cost_and_unpriced():
    prices = {"a": ModelPrice(input=1.0, output=2.0)}
    calls = [
        CallUsage(model="a", input_tokens=1000, output_tokens=500, cache_read_tokens=2000, latency_s=1.0),
        CallUsage(model="a", input_tokens=1000, output_tokens=500, cache_write_tokens=4000, latency_s=2.0),
        CallUsage(model="b", input_tokens=7, output_tokens=7, latency_s=0.25),
    ]
    u = summarize(calls, prices, wall_time_s=2.5)
    assert (u.calls, u.input_tokens, u.output_tokens) == (3, 2007, 1007)
    assert (u.cache_read_tokens, u.cache_write_tokens) == (2000, 4000)
    assert u.total_input_tokens == 8007
    assert u.unpriced_calls == 1
    # 2000*1 + 1000*2 + 2000*0.1 + 4000*1.25 = 9200 per-MTok units
    assert u.est_usd == pytest.approx(9200 / 1_000_000)
    assert (u.model_time_s, u.wall_time_s) == (3.25, 2.5)
    assert u.by_model["a"].input_tokens == 2000 and u.by_model["b"].output_tokens == 7


def test_record_call_is_noop_outside_recording():
    record_call(CallUsage(model="x"))  # must not raise or leak into a later recording
    with recording() as calls:
        record_call(CallUsage(model="y"))
    assert [c.model for c in calls] == ["y"]


async def test_recording_sees_gathered_tasks_and_isolates_concurrent_reviews():
    async def work(tag: str, n: int) -> list[CallUsage]:
        with recording() as calls:
            async def one():
                await asyncio.sleep(0)
                record_call(CallUsage(model=tag))
            await asyncio.gather(*(one() for _ in range(n)))
        return calls

    a, b = await asyncio.gather(work("a", 3), work("b", 2))
    assert [c.model for c in a] == ["a"] * 3
    assert [c.model for c in b] == ["b"] * 2


# ------------------------------------------------------------------ AnthropicLLM


async def test_anthropic_llm_records_usage():
    async def create(**kw):
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input={"findings": []})],
            usage=SimpleNamespace(input_tokens=120, output_tokens=30, cache_read_input_tokens=900,
                                  cache_creation_input_tokens=None),
        )

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm._sem = asyncio.Semaphore(1)
    with recording() as calls:
        await llm.call_tool(model="claude-sonnet-5-5", system="s", user="u", tool={"name": "report_findings"})
    [c] = calls
    assert (c.model, c.input_tokens, c.output_tokens, c.cache_read_tokens, c.cache_write_tokens) == (
        "claude-sonnet-5-5", 120, 30, 900, 0)
    assert c.latency_s >= 0


# ------------------------------------------------------------------ pipeline + output


async def test_pipeline_aggregates_usage(settings):
    raw = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "bug",
           "title": "t", "detail": "d", "confidence": 0.9}
    llm = MeteredLLM({"correctness": [raw]})
    result = await review(await _pr(settings), llm, settings)
    u = result.usage
    assert u.calls == len(llm.calls) == result.stats["agent_calls"] + 1  # + one verifier call
    assert u.input_tokens == 400 * u.calls and u.cache_read_tokens == 600 * u.calls
    assert set(u.by_model) == {settings.reviewer_model, settings.verifier_model}
    assert u.est_usd > 0 and u.unpriced_calls == 0
    assert u.wall_time_s >= 0


async def test_plain_fake_llm_reports_zero_usage(settings):
    result = await review(await _pr(settings), FakeLLM({}), settings)
    assert result.usage.calls == 0 and result.usage.est_usd == 0


def test_format_usage_line():
    u = Usage(calls=12, input_tokens=17_200, cache_read_tokens=31_000, output_tokens=3_400,
              est_usd=0.2149, wall_time_s=38.42)
    assert format_usage(u) == "12 calls · 48.2k in (31.0k cached) · 3.4k out · ~$0.21 · 38.4s"
    assert format_usage(Usage(calls=1, input_tokens=5, unpriced_calls=1)) == (
        "1 call · 5 in · 0 out · ~$0.00 (+1 unpriced) · 0.0s")


def test_summary_and_cli_show_usage_footer(capsys):
    usage = Usage(calls=2, input_tokens=1500, output_tokens=200, est_usd=0.01, wall_time_s=1.0)
    result = ReviewResult(usage=usage)
    assert format_summary(result).endswith(f"<sub>💸 {format_usage(usage)}</sub>")
    assert "💸" not in format_summary(ReviewResult())  # no calls, no footer
    print_result(result)
    assert f"usage: {format_usage(usage)}" in capsys.readouterr().out
