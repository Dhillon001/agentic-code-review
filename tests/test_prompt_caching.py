from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.agents.llm import AnthropicLLM, as_text, cached_text, text_block
from reviewer.agents.specialists import SPECIALISTS
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext
from reviewer.pipeline import review

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", body="why", files=ctx)


def _specialist_calls(llm: FakeLLM) -> list[tuple[str, list]]:
    return [(s, u) for name, s, u in zip(llm.calls, llm.systems, llm.users) if name == "report_findings"]


async def test_shared_context_is_identical_cached_prefix(settings):
    llm = FakeLLM({})
    await review(await _pr(settings), llm, settings)
    calls = _specialist_calls(llm)
    assert len(calls) == len(SPECIALISTS)

    # System prompt and the leading context block are byte-identical across specialists...
    assert len({s for s, _ in calls}) == 1
    firsts = [u[0] for _, u in calls]
    assert all(b == firsts[0] for b in firsts)
    assert firsts[0]["cache_control"] == {"type": "ephemeral"}
    assert "=== FILE: app/pagination.py" in firsts[0]["text"]

    # ...and the specialist focus comes after the breakpoint, uncached.
    for _, user in calls:
        assert len(user) == 2 and "cache_control" not in user[1]
    names = {u[1]["text"].split("`")[1] for _, u in calls}
    assert names == {s.name for s in SPECIALISTS}


async def test_team_rules_stay_in_shared_system(settings):
    s = settings.model_copy(update={"rules": "No raw SQL."})
    llm = FakeLLM({})
    await review(await _pr(s), llm, s)
    systems = {sys for sys, _ in _specialist_calls(llm)}
    assert len(systems) == 1 and "No raw SQL." in systems.pop()


class _OrderLLM(FakeLLM):
    """Records when each specialist call starts and finishes, yielding mid-call."""

    def __init__(self):
        super().__init__({})
        self.events: list[str] = []

    async def call_tool(self, **kw):
        if kw["tool"]["name"] == "report_findings":
            agent = as_text(kw["user"]).split("`")[1]
            self.events.append(f"start:{agent}")
            await asyncio.sleep(0)
            self.events.append(f"end:{agent}")
        return await super().call_tool(**kw)


async def test_cache_warmup_runs_first_specialist_alone(settings):
    llm = _OrderLLM()
    await review(await _pr(settings), llm, settings)
    first = SPECIALISTS[0].name
    assert llm.events[:2] == [f"start:{first}", f"end:{first}"]


async def test_cache_warmup_can_be_disabled(settings):
    s = settings.model_copy(update={"cache_warmup": False})
    llm = _OrderLLM()
    await review(await _pr(s), llm, s)
    assert all(e.startswith("start:") for e in llm.events[: len(SPECIALISTS)])


async def test_findings_unaffected_by_block_content(settings):
    raw = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "off-by-one",
           "title": "t", "detail": "d", "confidence": 0.9}
    result = await review(await _pr(settings), FakeLLM({"security": [raw]}), settings)
    assert [(f.file, f.line, f.agent) for f in result.inline] == [("app/pagination.py", 3, "security")]


def test_as_text_accepts_str_and_blocks():
    assert as_text("plain") == "plain"
    assert as_text([cached_text("a"), text_block("b")]) == "a\n\nb"


async def test_anthropic_llm_passes_blocks_through():
    sent: dict = {}

    async def create(**kw):
        sent.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"findings": []})])

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    llm._sem = asyncio.Semaphore(1)
    blocks = [cached_text("ctx"), text_block("focus")]
    out = await llm.call_tool(model="m", system="s", user=blocks, tool={"name": "report_findings"})
    assert out == {"findings": []}
    assert sent["messages"] == [{"role": "user", "content": blocks}]
    assert sent["system"] == "s"
