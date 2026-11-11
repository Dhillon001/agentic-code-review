from __future__ import annotations

import logging

import pytest
from conftest import FakeLLM
from pydantic import ValidationError

from reviewer.agents.llm import as_text
from reviewer.agents.specialists import SPECIALISTS, build_specialists
from reviewer.config import CustomAgent
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext, Severity
from reviewer.pipeline import review
from reviewer.repo_config import MAX_CUSTOM_AGENTS, apply_repo_config, parse_repo_config

BUILTINS = [s.name for s in SPECIALISTS]

CONFIG = """\
agents:
  - name: accessibility
    focus: Missing alt text, unlabeled form controls, keyboard traps.
  - name: data-privacy
    focus: |
      PII written to logs
      or sent to third parties.
    severity_floor: high
disable: [performance]
"""

DIFF = """\
diff --git a/app/views.py b/app/views.py
--- a/app/views.py
+++ b/app/views.py
@@ -1,1 +1,3 @@
 def show(user):
+    log.info("user %s", user.email)
+    return render(user)
"""

FILES = {"app/views.py": 'def show(user):\n    log.info("user %s", user.email)\n    return render(user)\n'}


def finding(line: int, severity: str, category: str = "pii-in-logs") -> dict:
    return {"file": "app/views.py", "line": line, "severity": severity, "category": category,
            "title": "t", "detail": "d", "confidence": 0.9}


async def _pr(settings):
    pr = PRContext(repo="o/r", number=1, head_sha="h", files=[])
    pr.files = await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings)
    return pr


# ---------------------------------------------------------------- parsing


def test_parse_agents_and_disable():
    cfg = parse_repo_config(CONFIG)
    assert [a.name for a in cfg.agents] == ["accessibility", "data-privacy"]
    assert cfg.agents[1].focus == "PII written to logs or sent to third parties."
    assert cfg.agents[0].severity_floor is None and cfg.agents[1].severity_floor == Severity.HIGH
    assert cfg.disable == ["performance"]


def test_disable_accepts_a_single_string_and_normalises_case():
    assert parse_repo_config("disable: Performance").disable == ["performance"]


@pytest.mark.parametrize("name", ["", "Has Space", "1st", "x" * 41, "semi;colon"])
def test_invalid_agent_names_rejected(name):
    with pytest.raises(ValidationError):
        CustomAgent(name=name, focus="something")


def test_names_are_lowercased_and_empty_focus_rejected():
    assert CustomAgent(name=" API-Contracts ", focus="x").name == "api-contracts"
    with pytest.raises(ValidationError):
        CustomAgent(name="ok", focus="   ")


def test_bad_agent_entries_are_skipped_not_fatal(caplog):
    text = """\
agents:
  - name: Bad Name
    focus: x
  - just a string
  - name: no-focus
  - name: good
    focus: Check feature flags are cleaned up.
    severity_floor: extreme
  - name: fine
    focus: Check feature flags are cleaned up.
min_severity: high
"""
    with caplog.at_level(logging.WARNING):
        cfg = parse_repo_config(text)
    assert [a.name for a in cfg.agents] == ["fine"]
    assert cfg.min_severity == Severity.HIGH  # the rest of the file still applies
    assert caplog.text.count("ignoring agents[") == 4


def test_agents_not_a_list_falls_back_to_default():
    cfg = parse_repo_config("agents: {name: x, focus: y}\nmax_comments: 3\n")
    assert cfg.agents == [] and cfg.max_comments == 3


def test_repeated_agent_name_last_wins_and_count_capped():
    text = "agents:\n" + "".join(f"  - {{name: a{i}, focus: f{i}}}\n" for i in range(MAX_CUSTOM_AGENTS + 3))
    text += "  - {name: a0, focus: replaced}\n"
    cfg = parse_repo_config(text)
    assert [a.name for a in cfg.agents] == [f"a{i}" for i in range(MAX_CUSTOM_AGENTS)]
    assert cfg.agents[0].focus == "replaced"


def test_apply_merges_over_global_settings(settings):
    settings = settings.model_copy(update={
        "custom_agents": [CustomAgent(name="data-privacy", focus="old"), CustomAgent(name="docs", focus="d")],
        "disable_agents": ["tests"],
    })
    merged = apply_repo_config(settings, parse_repo_config(CONFIG))
    assert [(a.name, a.focus[:3]) for a in merged.custom_agents] == [
        ("docs", "d"), ("accessibility", "Mis"), ("data-privacy", "PII")]
    assert merged.disable_agents == ["tests", "performance"]


# ---------------------------------------------------------------- building the roster


def test_default_roster_is_the_builtins(settings):
    assert build_specialists(settings) == SPECIALISTS


def test_build_adds_custom_and_drops_disabled(settings):
    merged = apply_repo_config(settings, parse_repo_config(CONFIG))
    names = [s.name for s in build_specialists(merged)]
    assert names == ["correctness", "security", "tests", "accessibility", "data-privacy"]


def test_unknown_disable_is_ignored_with_warning(settings, caplog):
    settings = settings.model_copy(update={"disable_agents": ["perfomance", "security"]})
    with caplog.at_level(logging.WARNING):
        names = [s.name for s in build_specialists(settings)]
    assert names == ["correctness", "performance", "tests"]
    assert "unknown agent in disable list: 'perfomance'" in caplog.text


@pytest.mark.parametrize("name", ["security", "secrets", "verifier"])
def test_custom_agent_cannot_shadow_builtin_or_reserved(settings, caplog, name):
    settings = settings.model_copy(update={"custom_agents": [CustomAgent(name=name, focus="sneaky")]})
    with caplog.at_level(logging.WARNING):
        roster = build_specialists(settings)
    assert all(s.focus != "sneaky" for s in roster)
    assert [s.name for s in roster] == BUILTINS
    assert "skipping custom agent" in caplog.text


def test_custom_agents_can_be_disabled_too(settings):
    settings = settings.model_copy(update={
        "custom_agents": [CustomAgent(name="docs", focus="d")], "disable_agents": ["docs"]})
    assert [s.name for s in build_specialists(settings)] == BUILTINS


# ---------------------------------------------------------------- end to end


async def test_pipeline_runs_custom_agents_and_skips_disabled(settings):
    settings = apply_repo_config(settings, parse_repo_config(CONFIG))
    llm = FakeLLM({"accessibility": [finding(3, "medium", "a11y")], "performance": [finding(2, "high")]})
    result = await review(await _pr(settings), llm, settings)

    agents_called = [as_text(u).split("You are the `", 1)[1].split("`", 1)[0]
                     for u, tool in zip(llm.users, llm.calls) if tool == "report_findings"]
    assert sorted(agents_called) == ["accessibility", "correctness", "data-privacy", "security", "tests"]
    assert result.stats["agent_calls"] == 5
    focus = next(as_text(u) for u in llm.users if "`data-privacy` reviewer" in as_text(u))
    assert "PII written to logs or sent to third parties." in focus
    assert [(f.agent, f.line) for f in result.inline] == [("accessibility", 3)]


async def test_severity_floor_drops_low_findings_from_that_agent_only(settings):
    settings = apply_repo_config(settings, parse_repo_config(CONFIG + "min_severity: low\n"))
    llm = FakeLLM({
        "data-privacy": [finding(2, "medium"), finding(3, "critical", "pii-leak")],
        "correctness": [finding(2, "low", "off-by-one")],
    })
    result = await review(await _pr(settings), llm, settings)
    got = sorted((f.agent, f.severity.value) for f in [*result.inline, *result.summary_only])
    assert got == [("correctness", "low"), ("data-privacy", "critical")]
    assert result.stats["raw_findings"] == 2  # the medium one never reached the verifier


async def test_severity_floor_holds_after_verifier_downgrade(settings):
    settings = apply_repo_config(settings, parse_repo_config(CONFIG + "min_severity: low\n"))

    class Downgrading(FakeLLM):
        async def call_tool(self, **kw):
            out = await super().call_tool(**kw)
            for v in out.get("verdicts", []):
                v["severity"] = "low"
            return out

    llm = Downgrading({"data-privacy": [finding(2, "high")], "correctness": [finding(3, "high", "bug")]})
    result = await review(await _pr(settings), llm, settings)
    assert [(f.agent, f.severity.value) for f in result.inline] == [("correctness", "low")]


async def test_disabling_every_agent_makes_no_model_calls(settings):
    settings = settings.model_copy(update={"disable_agents": BUILTINS})
    llm = FakeLLM({})
    result = await review(await _pr(settings), llm, settings)
    assert llm.calls == [] and result.stats["agent_calls"] == 0
