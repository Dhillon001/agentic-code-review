from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from conftest import FakeLLM

from evals.run_evals import load_case
from reviewer.agents.llm import as_text
from reviewer.agents.specialists import SPECIALISTS, build_specialists
from reviewer.config import CustomAgent, Settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext
from reviewer.pipeline import review
from reviewer.repo_config import apply_repo_config, parse_repo_config
from reviewer.usage import CallUsage, ModelPrice, record_call

CASE = Path(__file__).parents[1] / "evals" / "cases" / "off-by-one-pagination"
RAW = {"file": "app/pagination.py", "line": 3, "severity": "high", "category": "bug",
       "title": "t", "detail": "d", "confidence": 0.9}


async def _pr(settings) -> PRContext:
    _, diff, files = load_case(CASE)
    ctx = await build_context(InMemorySource(files), parse_unified_diff(diff), settings)
    return PRContext(repo="t/t", title="t", body="why", files=ctx)


class ModelLLM(FakeLLM):
    """Records (agent, model) per specialist call and meters 1k input tokens per call."""

    def __init__(self, findings_by_agent=None, **kw):
        super().__init__(findings_by_agent or {}, **kw)
        self.routes: list[tuple[str, str]] = []
        self.events: list[str] = []

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        if tool["name"] == "report_findings":
            agent = as_text(user).split("You are the `", 1)[1].split("`", 1)[0]
            self.routes.append((agent, model))
            self.events.append(f"start:{agent}")
            await asyncio.sleep(0)
            self.events.append(f"end:{agent}")
        else:
            self.routes.append(("verifier", model))
        record_call(CallUsage(model=model, input_tokens=1000, output_tokens=100))
        return await super().call_tool(model=model, system=system, user=user, tool=tool, max_tokens=max_tokens)


# ------------------------------------------------------------------ settings


def test_model_for_falls_back_to_reviewer_model(settings):
    s = settings.model_copy(update={"agent_models": {"security": "claude-opus-5-5"}})
    assert s.model_for("security") == "claude-opus-5-5"
    assert s.model_for("tests") == s.reviewer_model


def test_agent_models_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_MODELS", '{"Security": "claude-opus-5-5", "tests": "claude-haiku-4-5"}')
    s = Settings(_env_file=None)
    assert s.agent_models == {"security": "claude-opus-5-5", "tests": "claude-haiku-4-5"}


def test_invalid_entries_are_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        s = Settings(_env_file=None, agent_models={
            "security": "claude-opus-5-5", "Bad Name!": "x", "tests": "", "perf": "rm -rf /", "docs": ["a"]})
    assert s.agent_models == {"security": "claude-opus-5-5"}
    assert caplog.text.count("ignoring") == 4


# ------------------------------------------------------------------ .acr.yml


def test_repo_models_override_global_per_name(settings):
    cfg = parse_repo_config("models:\n  security: claude-opus-5-5\n  correctness: claude-haiku-4-5\n")
    assert cfg.models == {"security": "claude-opus-5-5", "correctness": "claude-haiku-4-5"}
    base = settings.model_copy(update={"agent_models": {"security": "claude-sonnet-5-5", "tests": "m-1"}})
    merged = apply_repo_config(base, cfg)
    assert merged.agent_models == {"security": "claude-opus-5-5", "tests": "m-1", "correctness": "claude-haiku-4-5"}


def test_bad_models_field_falls_back_without_breaking_config():
    cfg = parse_repo_config("models: [opus]\nmax_comments: 3\n")
    assert cfg.models == {} and cfg.max_comments == 3


def test_unknown_agent_route_warns(settings, caplog):
    s = settings.model_copy(update={"agent_models": {"securty": "claude-opus-5-5"}})
    with caplog.at_level(logging.WARNING):
        assert [sp.name for sp in build_specialists(s)] == [sp.name for sp in SPECIALISTS]
    assert "securty" in caplog.text


def test_route_for_disabled_agent_is_not_unknown(settings, caplog):
    s = settings.model_copy(update={"agent_models": {"performance": "m"}, "disable_agents": ["performance"]})
    with caplog.at_level(logging.WARNING):
        build_specialists(s)
    assert "model route" not in caplog.text


# ------------------------------------------------------------------ pipeline


async def test_specialists_use_routed_models_and_verifier_unchanged(settings):
    s = settings.model_copy(update={
        "agent_models": {"security": "claude-opus-5-5", "docs": "claude-haiku-4-5"},
        "custom_agents": [CustomAgent(name="docs", focus="Docstrings.")],
    })
    llm = ModelLLM({"correctness": [RAW]})
    await review(await _pr(s), llm, s)
    routes = dict(llm.routes)
    assert routes["security"] == "claude-opus-5-5"
    assert routes["docs"] == "claude-haiku-4-5"
    assert routes["correctness"] == routes["tests"] == routes["performance"] == s.reviewer_model
    assert routes["verifier"] == s.verifier_model


async def test_usage_and_cost_are_tracked_per_model(settings):
    s = settings.model_copy(update={
        "agent_models": {"security": "claude-opus-5-5", "tests": "claude-haiku-4-5", "performance": "my-model"},
        "model_prices": {"claude-opus": ModelPrice(input=5, output=25), "claude-sonnet": ModelPrice(input=3, output=15),
                         "claude-haiku": ModelPrice(input=1, output=5)},
    })
    llm = ModelLLM({"correctness": [RAW]})
    result = await review(await _pr(s), llm, s)
    u = result.usage
    # correctness on sonnet; security + verifier on opus; tests on haiku; performance unpriced
    assert set(u.by_model) == {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5", "my-model"}
    assert u.by_model["claude-opus-5-5"].input_tokens == 2000
    assert u.by_model["claude-haiku-4-5"].input_tokens == 1000
    assert u.unpriced_calls == 1
    per_call = {"sonnet": (1000 * 3 + 100 * 15), "opus": (1000 * 5 + 100 * 25), "haiku": (1000 * 1 + 100 * 5)}
    expected = (per_call["sonnet"] + 2 * per_call["opus"] + per_call["haiku"]) / 1_000_000
    assert u.est_usd == pytest.approx(expected)


async def test_cache_warmup_once_per_model(settings):
    # correctness + performance share the default model; security and tests each get their own
    s = settings.model_copy(update={"agent_models": {"security": "m-a", "tests": "m-b"}})
    llm = ModelLLM()
    await review(await _pr(s), llm, s)
    names = [sp.name for sp in SPECIALISTS]
    warmers = {"correctness", "security", "tests"}
    n = len(warmers)
    assert {e.split(":")[1] for e in llm.events[:n]} == warmers and all(e.startswith("start:") for e in llm.events[:n])
    # nobody else starts until every warmer has finished
    first_other = min(llm.events.index(f"start:{a}") for a in names if a not in warmers)
    assert all(llm.events.index(f"end:{w}") < first_other for w in warmers)


async def test_default_warmup_unchanged_without_routes(settings):
    llm = ModelLLM()
    await review(await _pr(settings), llm, settings)
    first = SPECIALISTS[0].name
    assert llm.events[:2] == [f"start:{first}", f"end:{first}"]
