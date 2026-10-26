from __future__ import annotations

import subprocess

import pytest
from conftest import FakeLLM

from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import Severity
from reviewer.pipeline import review
from reviewer.repo_config import (
    RepoConfig,
    apply_repo_config,
    filter_ignored,
    load_repo_config,
    parse_repo_config,
    path_ignored,
)
from reviewer.service import context_from_github, context_from_local

FULL = """\
ignore:
  - "docs/**"
  - "*.snap"
  - migrations/
min_severity: high
max_comments: 3
rules: |
  - No raw SQL in request handlers.
"""


def test_parse_full_config():
    cfg = parse_repo_config(FULL)
    assert cfg.ignore == ["docs/**", "*.snap", "migrations/"]
    assert cfg.min_severity is Severity.HIGH
    assert cfg.max_comments == 3
    assert cfg.rules == "- No raw SQL in request handlers."


def test_rules_as_list_and_single_ignore_string():
    cfg = parse_repo_config("ignore: '*.md'\nrules:\n  - use httpx, not requests\n  - ''\n  - log, don't print\n")
    assert cfg.ignore == ["*.md"]
    assert cfg.rules == "- use httpx, not requests\n- log, don't print"


@pytest.mark.parametrize("text", [None, "", "   \n", "# only a comment\n", "just a string", "[1, 2]", "a: [unclosed"])
def test_missing_empty_or_broken_config_is_default(text):
    assert parse_repo_config(text) == RepoConfig()


def test_invalid_fields_fall_back_individually():
    cfg = parse_repo_config("min_severity: urgent\nmax_comments: -1\nrules: keep it simple\nunknown: 1\n")
    assert cfg.min_severity is None and cfg.max_comments is None
    assert cfg.rules == "keep it simple"


def test_rules_are_capped():
    assert len(parse_repo_config("rules: " + "x" * 10_000).rules) == 4000


def test_apply_merges_over_settings(settings):
    base = settings.model_copy(update={"ignore": ["vendor/"]})
    merged = apply_repo_config(base, parse_repo_config(FULL))
    assert merged.min_severity == "high" and merged.max_comments == 3
    assert merged.ignore == ["vendor/", "docs/**", "*.snap", "migrations/"]
    assert merged.rules.startswith("- No raw SQL")
    assert base.max_comments == 10 and base.rules == ""  # original untouched


def test_apply_empty_config_keeps_settings(settings):
    merged = apply_repo_config(settings, RepoConfig())
    assert merged.model_dump() == settings.model_dump()


@pytest.mark.parametrize(
    ("path", "ignored"),
    [
        ("docs/guide.md", True),
        ("docs/deep/nested/x.md", True),
        ("src/docs.py", False),
        ("tests/__snapshots__/a.snap", True),
        ("db/migrations/0001_init.py", True),
        ("migrations.py", False),
        ("src/app.py", False),
    ],
)
def test_path_ignored(path, ignored):
    assert path_ignored(path, ["docs/**", "*.snap", "migrations/"]) is ignored


def test_path_ignored_anchored_dir_and_dot_slash():
    assert path_ignored("web/static/app.js", ["web/static/"])
    assert not path_ignored("api/web/static/app.js", ["web/static/"])
    assert path_ignored("gen/x.py", ["./gen/*"])


DIFF = """\
diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,2 +1,3 @@
 import sqlite3
+QUERY = "SELECT * FROM users WHERE id = %s"
 conn = None
diff --git a/docs/notes.md b/docs/notes.md
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -1 +1,2 @@
 # Notes
+more
"""


def test_filter_ignored():
    diffs = parse_unified_diff(DIFF)
    assert [d.path for d in filter_ignored(diffs, ["docs/**"])] == ["app/db.py"]
    assert filter_ignored(diffs, []) == diffs


async def test_load_from_source():
    cfg = await load_repo_config(InMemorySource({".acr.yml": "max_comments: 2\n"}))
    assert cfg.max_comments == 2
    assert await load_repo_config(InMemorySource({})) == RepoConfig()


# ---------------------------------------------------------------- pipeline + service wiring

FINDING = {"file": "app/db.py", "line": 2, "severity": "medium", "category": "team-rule",
           "title": "raw SQL", "detail": "handlers must use the repo layer", "confidence": 0.9}


async def _pr(settings):
    from reviewer.context.retriever import build_context
    from reviewer.models import PRContext

    files = {"app/db.py": 'import sqlite3\nQUERY = "SELECT * FROM users WHERE id = %s"\nconn = None\n'}
    ctx = await build_context(InMemorySource(files), filter_ignored(parse_unified_diff(DIFF), ["docs/"]), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


async def test_rules_reach_specialists_and_verifier(settings):
    s = apply_repo_config(settings, parse_repo_config(FULL))
    llm = FakeLLM({"correctness": [FINDING]})
    await review(await _pr(s), llm, s)
    assert llm.calls.count("report_findings") == 4 and "report_verdicts" in llm.calls
    assert all("No raw SQL in request handlers." in sys for sys in llm.systems)
    assert all("Team conventions" in sys for sys in llm.systems)


async def test_no_rules_no_team_section(settings):
    llm = FakeLLM({})
    await review(await _pr(settings), llm, settings)
    assert llm.systems and not any("Team conventions" in sys for sys in llm.systems)


async def test_repo_min_severity_and_cap_applied(settings):
    s = apply_repo_config(settings, parse_repo_config("min_severity: high\n"))
    result = await review(await _pr(s), FakeLLM({"correctness": [FINDING]}), s)
    assert result.inline == [] and result.stats["confirmed"] == 1

    s = apply_repo_config(settings, parse_repo_config("max_comments: 0\n"))
    result = await review(await _pr(s), FakeLLM({"correctness": [FINDING]}), s)
    assert result.stats["posted"] == 0


class FakeGitHub:
    """Just enough of GitHubClient for context_from_github — no network."""

    def __init__(self, files_by_ref: dict[str, dict[str, str]]):
        self.files_by_ref = files_by_ref
        self.reads: list[tuple[str, str, str]] = []

    async def get_pr(self, repo, number):
        return {"title": "t", "body": "", "head": {"sha": "head1", "repo": {"full_name": repo}},
                "base": {"sha": "base1"}}

    async def get_pr_diff(self, repo, number):
        return DIFF

    async def get_file(self, repo, path, ref):
        self.reads.append((repo, path, ref))
        return self.files_by_ref.get(ref, {}).get(path)

    async def search_code(self, query, repo, limit=5):
        return []


async def test_github_reads_config_from_base_not_head(settings):
    gh = FakeGitHub({
        "base1": {".acr.yml": "ignore: [docs/]\nrules: base rule\n"},
        "head1": {".acr.yml": "ignore: ['**']\nmax_comments: 0\n", "app/db.py": "x\n"},
    })
    pr, merged = await context_from_github(gh, "o/r", 1, settings)
    assert [fc.path for fc in pr.files] == ["app/db.py"]
    assert merged.rules == "base rule" and merged.max_comments == settings.max_comments
    assert ("o/r", ".acr.yml", "base1") in gh.reads and ("o/r", ".acr.yml", "head1") not in gh.reads


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def test_local_reads_config_and_ignores(tmp_path, settings):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("a = 1\n")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "out.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    (tmp_path / "app.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "gen" / "out.py").write_text("x = 1\ny = 2\n")
    (tmp_path / ".acr.yml").write_text("ignore: [gen/, .acr.yml]\nmin_severity: critical\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "change")

    pr, merged = await context_from_local(tmp_path, "main", "HEAD", settings)
    assert [fc.path for fc in pr.files] == ["app.py"]
    assert merged.min_severity == "critical"
