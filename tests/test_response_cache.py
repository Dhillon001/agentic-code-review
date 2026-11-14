from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer import cli
from reviewer.agents.cache import CachingLLM, cache_key
from reviewer.agents.llm import cached_text, text_block
from reviewer.agents.resilience import ResilientLLM, RetryPolicy
from reviewer.config import Settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext, ReviewResult
from reviewer.output.github_review import format_summary
from reviewer.pipeline import review
from reviewer.service import make_llm
from reviewer.usage import (
    CallUsage,
    Usage,
    format_usage,
    record_call,
    recording,
    summarize,
)

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"
TOOL = {"name": "report_findings", "input_schema": {"type": "object", "properties": {}}}
REAL = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "off-by-one",
        "title": "page is 1-indexed", "detail": "page=1 skips the first page", "confidence": 0.9}


class Echo:
    """Counts calls and returns a fresh dict each time, reporting usage like AnthropicLLM."""

    def __init__(self):
        self.calls = 0

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        self.calls += 1
        record_call(CallUsage(model=model, input_tokens=100, output_tokens=10))
        return {"n": self.calls, "model": model}


class Failing:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    async def call_tool(self, **kw):
        self.calls += 1
        raise self.exc


class Metered(FakeLLM):
    async def call_tool(self, **kw):
        record_call(CallUsage(model=kw["model"], input_tokens=1000, output_tokens=100))
        return await super().call_tool(**kw)


def _kw(**over):
    return {"model": "m", "system": "sys", "user": "hello", "tool": TOOL, **over}


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


# ------------------------------------------------------------------ key


def test_key_covers_model_system_user_and_tool():
    base = cache_key(**_kw())
    assert cache_key(**_kw()) == base
    assert cache_key(**_kw(model="m2")) != base
    assert cache_key(**_kw(system="sys2")) != base
    assert cache_key(**_kw(user="hello!")) != base
    assert cache_key(**_kw(tool={**TOOL, "name": "report_verdicts"})) != base
    schema = {**TOOL, "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
    assert cache_key(**_kw(tool=schema)) != base


def test_key_ignores_cache_control_and_dict_order():
    a = cache_key(**_kw(user=[cached_text("ctx"), text_block("focus")]))
    b = cache_key(**_kw(user=[text_block("ctx"), text_block("focus")]))
    assert a == b
    reordered = {"input_schema": TOOL["input_schema"], "name": TOOL["name"]}
    assert cache_key(**_kw(tool=reordered)) == cache_key(**_kw())
    # block boundaries still matter: same text split differently is a different prompt
    assert cache_key(**_kw(user=[text_block("ctxfocus")])) != a


# ------------------------------------------------------------------ CachingLLM


async def test_second_identical_call_is_served_from_disk(tmp_path):
    inner = Echo()
    llm = CachingLLM(inner, tmp_path)
    first = await llm.call_tool(**_kw())
    second = await llm.call_tool(**_kw())
    assert first == second == {"n": 1, "model": "m"}
    assert inner.calls == 1 and (llm.hits, llm.misses) == (1, 1)
    # a fresh wrapper (a new process, an eval rerun) reads the same directory
    again = CachingLLM(Echo(), tmp_path)
    assert await again.call_tool(**_kw()) == first and again.inner.calls == 0
    # different input → miss
    assert (await llm.call_tool(**_kw(user="other")))["n"] == 2


async def test_entries_are_json_on_disk(tmp_path):
    llm = CachingLLM(Echo(), tmp_path)
    await llm.call_tool(**_kw())
    key = cache_key(**_kw())
    path = tmp_path / key[:2] / f"{key}.json"
    entry = json.loads(path.read_text())
    assert entry["output"] == {"n": 1, "model": "m"} and entry["tool"] == "report_findings"
    assert not list(tmp_path.rglob(".tmp-*"))  # atomic write left nothing behind


async def test_errors_are_never_cached(tmp_path):
    failing = Failing(RuntimeError("overloaded"))
    llm = CachingLLM(failing, tmp_path)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await llm.call_tool(**_kw())
    assert failing.calls == 2 and not list(tmp_path.rglob("*.json"))


async def test_errors_after_retries_are_not_cached(tmp_path):
    class Overloaded(Exception):
        status_code = 529

    failing = Failing(Overloaded())
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    llm = CachingLLM(ResilientLLM(failing, RetryPolicy(max_retries=2), sleep=sleep, rng=lambda: 0.0), tmp_path)
    with pytest.raises(Overloaded):
        await llm.call_tool(**_kw())
    assert failing.calls == 3 and not list(tmp_path.rglob("*.json"))


async def test_corrupt_or_foreign_entry_is_a_miss_and_gets_rewritten(tmp_path, caplog):
    key = cache_key(**_kw())
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    for bad in ("{not json", json.dumps({"v": 999, "output": {"n": 0}}), json.dumps({"v": 1, "output": [1]})):
        path.write_text(bad)
        inner = Echo()
        assert await CachingLLM(inner, tmp_path).call_tool(**_kw()) == {"n": 1, "model": "m"}
        assert inner.calls == 1
    assert json.loads(path.read_text())["output"] == {"n": 1, "model": "m"}


async def test_unwritable_cache_dir_does_not_fail_the_call(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    llm = CachingLLM(Echo(), blocker / "cache")  # parent is a file: mkdir fails
    assert await llm.call_tool(**_kw()) == {"n": 1, "model": "m"}
    assert "could not write" in caplog.text


async def test_cached_output_is_not_shared_mutable_state(tmp_path):
    llm = CachingLLM(Echo(), tmp_path)
    await llm.call_tool(**_kw())
    hit = await llm.call_tool(**_kw())
    hit["n"] = 99
    assert (await llm.call_tool(**_kw()))["n"] == 1


# ------------------------------------------------------------------ usage


def test_summarize_counts_hits_separately_and_free():
    calls = [CallUsage(model="claude-sonnet-5-5", input_tokens=1000, output_tokens=100),
             CallUsage(model="claude-sonnet-5-5", response_cached=True)]
    u = summarize(calls, {})
    assert (u.calls, u.response_cache_hits, u.input_tokens) == (1, 1, 1000)
    assert "1 call + 1 from response cache" in format_usage(u)
    assert "response cache" not in format_usage(summarize(calls[:1], {}))


def test_summary_footer_shown_when_every_call_was_a_hit():
    result = ReviewResult(usage=Usage(response_cache_hits=5))
    assert "0 calls + 5 from response cache" in format_summary(result)


async def test_rerun_of_a_review_is_free_and_identical(tmp_path, settings):
    settings = settings.model_copy(update={"cache_warmup": False})
    first_llm = Metered({"correctness": [REAL]})
    first = await review(await _pr(settings), CachingLLM(first_llm, tmp_path), settings)
    assert first.usage.calls == 5 and first.usage.response_cache_hits == 0  # 4 specialists + verifier

    second_llm = Metered({"correctness": []})  # would find nothing if actually asked
    second = await review(await _pr(settings), CachingLLM(second_llm, tmp_path), settings)
    assert second_llm.calls == []
    assert [(f.file, f.line) for f in second.inline] == [(f.file, f.line) for f in first.inline]
    assert second.usage.calls == 0 and second.usage.response_cache_hits == 5
    assert second.usage.input_tokens == 0 and second.usage.est_usd == 0


async def test_hits_are_recorded_inside_recording(tmp_path):
    llm = CachingLLM(Echo(), tmp_path)
    with recording() as calls:
        await llm.call_tool(**_kw())
        await llm.call_tool(**_kw())
    assert [c.response_cached for c in calls] == [False, True]


# ------------------------------------------------------------------ wiring


def test_settings_cache_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    assert Settings(_env_file=None).llm_cache_dir == str(tmp_path)
    monkeypatch.setenv("LLM_CACHE_DIR", "")
    assert Settings(_env_file=None).llm_cache_dir is None


def test_make_llm_wraps_only_when_configured(tmp_path):
    plain = make_llm(Settings(_env_file=None, anthropic_api_key="k"))
    assert isinstance(plain, ResilientLLM)
    cached = make_llm(Settings(_env_file=None, anthropic_api_key="k", llm_cache_dir=str(tmp_path)))
    assert isinstance(cached, CachingLLM) and isinstance(cached.inner, ResilientLLM)


def test_cli_cache_dir_flag_reaches_settings(monkeypatch, tmp_path):
    seen = {}

    async def fake_review_local(repo, base, head, settings):
        seen["dir"] = settings.llm_cache_dir
        return ReviewResult()

    monkeypatch.setattr(cli, "review_local", fake_review_local)
    assert cli.run(["local", "--cache-dir", str(tmp_path)]) == 0
    assert seen["dir"] == str(tmp_path)
