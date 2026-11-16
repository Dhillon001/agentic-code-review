"""`evals/new_case.py`: scaffold an eval case from a local commit — against real temporary git repos."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evals.new_case import CaseError, _ranges, build_case, main
from evals.run_evals import load_case
from reviewer.ingest.diff import parse_unified_diff


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "diff.noprefix", "true")  # hostile user config must not change the diff format
    (r / "app").mkdir()
    (r / "app" / "users.py").write_text("def find(db, name):\n    return db.run('SELECT 1', name)\n")
    (r / "app" / "util.py").write_text("X = 1\n")
    (r / "old.py").write_text("gone = True\n")
    _commit(r, "init")
    return r


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    return tmp_path / "cases"


def _change(repo: Path) -> str:
    (repo / "app" / "users.py").write_text(
        "def find(db, name):\n    q = f\"SELECT * FROM users WHERE name = '{name}'\"\n    return db.run(q)\n"
    )
    (repo / "app" / "new.py").write_text("def hello():\n    return 'hi'\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    (repo / "old.py").unlink()
    return _commit(repo, "Use f-string for user lookup\n\nbody text")


def test_builds_a_loadable_case(repo, cases):
    sha = _change(repo)
    dest = build_case(repo, sha, "sqli", cases_dir=cases, include=["app/util.py"])
    assert dest == cases / "sqli"

    meta, diff, files = load_case(dest)
    assert meta["title"] == "Use f-string for user lookup"
    assert meta["source"] == {"repo": "proj", "commit": sha}
    assert meta["expected"] == [{"file": "app/new.py", "line": 1, "category": "TODO"}]
    assert meta["_todo"]["commentable_lines"] == {"app/new.py": "1-2", "app/users.py": "1-3"}
    assert meta["_todo"]["excluded_binary_or_non_utf8"] == ["logo.png"]

    parsed = {fd.path: fd for fd in parse_unified_diff(diff)}
    assert set(parsed) == {"app/users.py", "app/new.py", "old.py"}  # binary dropped, deletion kept
    assert "logo.png" not in diff and "diff --git a/app/users.py b/app/users.py" in diff
    assert parsed["old.py"].status == "deleted" and parsed["app/new.py"].status == "added"
    # post-change contents, plus the --include context file; nothing for the deleted file
    assert set(files) == {"app/users.py", "app/new.py", "app/util.py"}
    assert files["app/users.py"] == (repo / "app" / "users.py").read_text()
    # every added line in the diff matches files/ — the same check tests/test_eval_cases.py runs
    for fd in parsed.values():
        for hunk in fd.hunks:
            added = iter(hunk.added_lines)
            for raw in hunk.lines:
                if raw.startswith("+"):
                    assert files[fd.path].splitlines()[next(added) - 1] == raw[1:]


def test_reads_the_commit_not_the_working_tree(repo, cases):
    sha = _change(repo)
    (repo / "app" / "users.py").write_text("LATER EDIT\n")
    _commit(repo, "later")
    _, _, files = load_case(build_case(repo, sha, "c", cases_dir=cases))
    assert "SELECT * FROM users" in files["app/users.py"]


def test_root_commit_and_title_override(tmp_path, cases):
    r = tmp_path / "fresh"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "a.py").write_text("print(1)\n")
    sha = _commit(r, "first")
    meta, diff, files = load_case(build_case(r, sha, "root", cases_dir=cases, title="Custom"))
    assert meta["title"] == "Custom" and files == {"a.py": "print(1)\n"}
    assert parse_unified_diff(diff)[0].status == "added"


def test_rename_keeps_new_path(repo, cases):
    _git(repo, "mv", "app/util.py", "app/helpers.py")
    (repo / "app" / "helpers.py").write_text("X = 1\nY = 2\n")
    sha = _commit(repo, "rename")
    _, diff, files = load_case(build_case(repo, sha, "mv", cases_dir=cases))
    (fd,) = parse_unified_diff(diff)
    assert fd.status == "renamed" and fd.path == "app/helpers.py" and set(files) == {"app/helpers.py"}


def test_refuses_to_overwrite(repo, cases):
    sha = _change(repo)
    (cases / "taken").mkdir(parents=True)
    (cases / "taken" / "case.json").write_text("{}")
    with pytest.raises(CaseError, match="refusing to overwrite"):
        build_case(repo, sha, "taken", cases_dir=cases)
    assert (cases / "taken" / "case.json").read_text() == "{}"


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "-x", "has space"])
def test_rejects_bad_names(repo, cases, name):
    with pytest.raises(CaseError, match="invalid case name"):
        build_case(repo, "HEAD", name, cases_dir=cases)
    assert not cases.exists()


def test_errors_leave_nothing_behind(repo, cases):
    with pytest.raises(CaseError, match="rev-parse"):
        build_case(repo, "deadbeef", "x", cases_dir=cases)
    sha = _change(repo)
    with pytest.raises(CaseError, match="--include nope.py"):
        build_case(repo, sha, "x", cases_dir=cases, include=["nope.py"])
    assert not (cases / "x").exists()


def test_refuses_merge_commits(repo, cases):
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.py").write_text("s = 1\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "main.py").write_text("m = 1\n")
    _commit(repo, "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    with pytest.raises(CaseError, match="merge commit"):
        build_case(repo, "HEAD", "m", cases_dir=cases)


def test_binary_only_commit_has_nothing_to_review(repo, cases):
    (repo / "img.bin").write_bytes(b"\0\1\2")
    sha = _commit(repo, "binary")
    with pytest.raises(CaseError, match="no reviewable text changes"):
        build_case(repo, sha, "b", cases_dir=cases)


def test_ranges():
    assert _ranges({1, 2, 3, 7, 9, 10}) == "1-3, 7, 9-10"
    assert _ranges({5}) == "5" and _ranges(set()) == ""


def test_main_prints_next_steps_and_exit_codes(repo, cases, capsys):
    sha = _change(repo)
    argv = ["--repo", str(repo), "--commit", sha, "--name", "cli", "--cases-dir", str(cases)]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "created" in out and "app/users.py: commentable lines 1-3" in out and "logo.png: skipped" in out
    assert json.loads((cases / "cli" / "case.json").read_text())["expected"][0]["category"] == "TODO"

    assert main(argv) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
