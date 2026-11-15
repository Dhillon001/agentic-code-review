"""`acr staged` and the pre-commit hook: review exactly what `git commit` would record."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FakeLLM

from reviewer import cli, service
from reviewer.gate import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from reviewer.ingest.sources import StagedGitSource
from reviewer.models import Finding, ReviewResult, Severity

ROOT = Path(__file__).resolve().parent.parent


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pay.py").write_text("def charge(amount):\n    return amount\n")
    (tmp_path / "app" / "util.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


# ---------------------------------------------------------------- source


async def test_reads_index_not_working_tree(repo):
    (repo / "app" / "pay.py").write_text("def charge(amount):\n    return amount * 100\n")
    _git(repo, "add", "app/pay.py")
    # an unstaged edit on top must not be reviewed
    (repo / "app" / "pay.py").write_text("def charge(amount):\n    return 'UNSTAGED'\n")
    (repo / "app" / "util.py").write_text("x = 2\n")  # modified but never staged

    src = StagedGitSource(repo)
    diff = src.diff()
    assert "amount * 100" in diff and "UNSTAGED" not in diff and "util.py" not in diff
    assert await src.read_file("app/pay.py") == "def charge(amount):\n    return amount * 100\n"
    assert await src.read_file("app/missing.py") is None


async def test_subdirectory_resolves_to_top_level(repo):
    _git(repo, "add", ".")
    src = StagedGitSource(repo / "app")
    assert src.repo == repo.resolve()
    assert await src.read_file("app/util.py") == "x = 1\n"
    assert await src.search("charge") == ["app/pay.py"]  # top-level-relative paths, whole repo
    assert await src.list_files() == ["app/pay.py", "app/util.py"]


async def test_new_file_before_first_commit(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "first.py").write_text("print('hi')\n")
    _git(tmp_path, "add", ".")
    src = StagedGitSource(tmp_path)
    assert "+print('hi')" in src.diff()
    assert await src.read_file("first.py") == "print('hi')\n"


def test_not_a_repository(tmp_path):
    with pytest.raises(ValueError, match="not a git repository"):
        StagedGitSource(tmp_path)


# ---------------------------------------------------------------- service


REAL = {"file": "app/pay.py", "line": 2, "severity": "high", "category": "units",
        "title": "cents vs dollars", "detail": "amount is already in cents", "confidence": 0.9}


async def test_review_staged_end_to_end(repo, settings, monkeypatch):
    (repo / "app" / "pay.py").write_text("def charge(amount):\n    return amount * 100\n")
    (repo / "uv.lock").write_text("version = 1\n")
    _git(repo, "add", ".")
    llm = FakeLLM({"correctness": [REAL]})
    monkeypatch.setattr(service, "make_llm", lambda s: llm)

    result = await service.review_staged(repo, settings)
    assert [(f.file, f.line) for f in result.inline] == [("app/pay.py", 2)]
    assert result.stats["skipped"] == 1  # lockfile is noise
    assert "amount * 100" in "".join(map(str, llm.users))


async def test_acr_yml_read_from_index(repo, settings):
    (repo / ".acr.yml").write_text("ignore: ['app/util.py']\nrules: ['Money is in cents']\n")
    (repo / "app" / "util.py").write_text("x = 2\n")
    _git(repo, "add", ".")
    ctx, merged = await service.context_from_staged(repo, settings)
    assert "app/util.py" in ctx.skipped
    assert "Money is in cents" in merged.rules
    assert ctx.title == "staged changes"


async def test_nothing_staged_needs_no_model_or_key(repo, settings, monkeypatch):
    def boom(s):
        raise AssertionError("must not build an LLM")

    monkeypatch.setattr(service, "make_llm", boom)
    (repo / "app" / "util.py").write_text("x = 2\n")  # unstaged only
    result = await service.review_staged(repo, settings.model_copy(update={"anthropic_api_key": None}))
    assert result.inline == [] and result.stats["files"] == 0

    _git(repo, "rm", "-q", "app/pay.py")  # deletions alone have nothing to review either
    result = await service.review_staged(repo, settings)
    assert result.inline == []


# ---------------------------------------------------------------- CLI


def test_cli_staged_gate(monkeypatch, capsys):
    finding = Finding(file="app/pay.py", line=2, severity=Severity.HIGH, category="units",
                      title="t", detail="d", agent="correctness")
    seen: dict = {}

    async def fake(repo, settings):
        seen["repo"] = repo
        return ReviewResult(inline=[finding])

    monkeypatch.setattr(cli, "review_staged", fake)
    assert cli.run(["staged", "--fail-on", "high"]) == EXIT_FINDINGS
    assert seen["repo"] == "."
    assert "app/pay.py:2" in capsys.readouterr().out
    assert cli.run(["staged", "--fail-on", "critical", "--format", "json"]) == EXIT_OK


def test_cli_staged_outside_repo_is_error(tmp_path, capsys):
    assert cli.run(["staged", "--repo", str(tmp_path)]) == EXIT_ERROR
    assert "not a git repository" in capsys.readouterr().err


# ---------------------------------------------------------------- hook manifest


def test_pre_commit_hook_manifest():
    hooks = yaml.safe_load((ROOT / ".pre-commit-hooks.yaml").read_text())
    [hook] = hooks
    assert hook["id"] == "acr"
    assert hook["entry"].split() == ["acr", "staged", "--fail-on", "high"]
    assert hook["language"] == "python" and hook["pass_filenames"] is False
    assert hook["stages"] == ["pre-commit"]
    # the entry must parse with the real CLI
    args = hook["entry"].split()[1:]
    cli_parser_ok = subprocess.run(
        [sys.executable, "-m", "reviewer.cli", *args, "--help"], capture_output=True, text=True, check=False
    )
    assert cli_parser_ok.returncode == 0 and "--fail-on" in cli_parser_ok.stdout
