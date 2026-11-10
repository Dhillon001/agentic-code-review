from __future__ import annotations

import logging

from conftest import FakeLLM

from reviewer.agents.llm import as_text
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext
from reviewer.pipeline import review
from reviewer.repo_config import (
    MAX_PATH_RULES,
    MAX_RULES_CHARS,
    PATH_RULES_HEADER,
    apply_repo_config,
    parse_repo_config,
    rules_for_paths,
)

CONFIG = """\
rules: Log, don't print.
paths:
  "src/api/**": Validate input with pydantic
  "*.sql":
    - Every migration is reversible
    - No SELECT *
"""

DIFF = """\
diff --git a/src/api/users.py b/src/api/users.py
--- a/src/api/users.py
+++ b/src/api/users.py
@@ -1,1 +1,2 @@
 def get(user_id):
+    return db.fetch(user_id)
diff --git a/lib/util.py b/lib/util.py
--- a/lib/util.py
+++ b/lib/util.py
@@ -1,1 +1,2 @@
 def add(a, b):
+    return a + b
"""

FILES = {"src/api/users.py": "def get(user_id):\n    return db.fetch(user_id)\n",
         "lib/util.py": "def add(a, b):\n    return a + b\n"}

API_FINDING = {"file": "src/api/users.py", "line": 2, "severity": "medium", "category": "team-rule",
               "title": "unvalidated input", "detail": "user_id is not validated", "confidence": 0.9}


def test_parse_paths_strings_and_lists():
    cfg = parse_repo_config(CONFIG)
    assert cfg.paths == {
        "src/api/**": "Validate input with pydantic",
        "*.sql": "- Every migration is reversible\n- No SELECT *",
    }


def test_bad_paths_fall_back_without_losing_other_fields():
    cfg = parse_repo_config("rules: keep it simple\npaths: [not, a, mapping]\n")
    assert cfg.paths == {} and cfg.rules == "keep it simple"
    cfg = parse_repo_config("paths:\n  'a/**': {nested: map}\n")
    assert cfg.paths == {}


def test_empty_entries_dropped_and_values_capped(caplog):
    cfg = parse_repo_config("paths:\n  './a/**': ''\n  '': rule\n  b/**: " + "x" * 10_000 + "\n")
    assert list(cfg.paths) == ["b/**"] and len(cfg.paths["b/**"]) == MAX_RULES_CHARS

    many = "paths:\n" + "".join(f"  d{i}/**: rule {i}\n" for i in range(MAX_PATH_RULES + 5))
    with caplog.at_level(logging.WARNING):
        assert len(parse_repo_config(many).paths) == MAX_PATH_RULES
    assert "paths" in caplog.text


def test_apply_merges_path_rules_over_settings(settings):
    base = settings.model_copy(update={"path_rules": {"src/api/**": "global", "web/**": "keep"}})
    merged = apply_repo_config(base, parse_repo_config(CONFIG))
    assert merged.path_rules["src/api/**"] == "Validate input with pydantic"
    assert merged.path_rules["web/**"] == "keep" and "*.sql" in merged.path_rules
    assert base.path_rules == {"src/api/**": "global", "web/**": "keep"}  # original untouched


def test_rules_for_paths_only_includes_matching_globs():
    paths = parse_repo_config(CONFIG).paths
    text = rules_for_paths("Log, don't print.", paths, ["src/api/users.py", "lib/util.py"])
    assert text.startswith("Log, don't print.")
    assert PATH_RULES_HEADER in text
    assert "`src/api/**` (applies to: src/api/users.py):\nValidate input with pydantic" in text
    assert "SELECT" not in text

    # basename globs match anywhere; no global rules and no match -> empty
    assert "No SELECT *" in rules_for_paths("", paths, ["db/migrations/001.sql"])
    assert rules_for_paths("", paths, ["lib/util.py"]) == ""
    assert rules_for_paths("global", {}, ["src/api/users.py"]) == "global"


async def _pr(settings):
    ctx = await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


def _calls(llm, tool):
    return [(s, as_text(u)) for c, s, u in zip(llm.calls, llm.systems, llm.users) if c == tool]


async def test_path_rules_only_reach_batches_with_matching_files(settings):
    s = apply_repo_config(settings.model_copy(update={"max_prompt_chars": 10}), parse_repo_config(CONFIG))
    llm = FakeLLM({"correctness": [API_FINDING]})
    result = await review(await _pr(s), llm, s)
    assert result.stats["batches"] == 2

    specialist_calls = _calls(llm, "report_findings")
    api = [sys for sys, user in specialist_calls if "=== FILE: src/api/users.py" in user]
    util = [sys for sys, user in specialist_calls if "=== FILE: lib/util.py" in user]
    assert len(api) == len(util) == 4
    assert all("Validate input with pydantic" in sys for sys in api)
    assert not any("Validate input with pydantic" in sys for sys in util)
    assert all("Log, don't print." in sys for sys in api + util)  # global rules still go everywhere
    assert len(set(api)) == 1 and len(set(util)) == 1  # byte-identical per batch -> prompt cache hits

    (verifier_sys, _), = _calls(llm, "report_verdicts")
    assert "Validate input with pydantic" in verifier_sys
    assert [f.file for f in result.inline] == ["src/api/users.py"]


async def test_one_batch_gets_rules_for_any_matching_file(settings):
    s = apply_repo_config(settings, parse_repo_config(CONFIG))
    llm = FakeLLM({})
    result = await review(await _pr(s), llm, s)
    assert result.stats["batches"] == 1
    assert all("Validate input with pydantic" in sys for sys, _ in _calls(llm, "report_findings"))


async def test_no_matching_paths_no_team_section(settings):
    s = apply_repo_config(settings, parse_repo_config("paths:\n  'web/**': only web\n"))
    llm = FakeLLM({})
    await review(await _pr(s), llm, s)
    assert llm.systems and not any("Team conventions" in sys or "only web" in sys for sys in llm.systems)
