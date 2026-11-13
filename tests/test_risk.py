from __future__ import annotations

import pytest
from conftest import FakeLLM

from reviewer.budget import estimate_cost
from reviewer.cli import format_text
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import FileContext, FileDiff, Hunk, PRContext, ReviewResult
from reviewer.output.github_review import (
    format_summary,
    max_files_note,
    unreviewed_notes,
)
from reviewer.output.markdown import format_markdown
from reviewer.pipeline import review
from reviewer.risk import (
    by_risk,
    cap_files,
    changed_line_count,
    is_sensitive_path,
    is_test_path,
    risk_score,
)
from reviewer.service import context_from_local
from reviewer.usage import ModelPrice


def _diff(path: str, added: int = 5, removed: int = 0, status: str = "modified") -> FileDiff:
    lines = [f"-old {i}" for i in range(removed)] + [f"+new {i}" for i in range(added)]
    hunk = Hunk(header="@@ -1 +1 @@", new_start=1, lines=lines, added_lines=list(range(1, added + 1)))
    return FileDiff(path=path, status=status, hunks=[hunk])


@pytest.mark.parametrize(
    "path",
    ["src/auth/views.py", "app/oauth.py", "lib/crypto_utils.go", "db/raw_sql.py", "store/mysql_store.py",
     "api/user_session.ts", "core/Permissions.java", "billing/payment.rb", "migrations/0004_add.py"],
)
def test_sensitive_paths(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize("path", ["src/author.py", "app/views.py", "README.md", "ui/button.tsx"])
def test_ordinary_paths(path):
    assert not is_sensitive_path(path)


@pytest.mark.parametrize(
    "path",
    ["tests/test_app.py", "pkg/handler_test.go", "src/app.test.ts", "web/__tests__/x.js", "src/Foo.spec.js",
     "src/test/java/FooTest.java", "tests/conftest.py", "test_util.py"],
)
def test_test_paths(path):
    assert is_test_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "src/latest.py", "contest/main.go", "src/attestation.py"])
def test_non_test_paths(path):
    assert not is_test_path(path)


def test_changed_lines_count_added_and_removed():
    assert changed_line_count(_diff("a.py", added=3, removed=2)) == 5


def test_score_ingredients():
    base = risk_score(_diff("app/views.py"))
    assert risk_score(_diff("app/auth.py")) > base  # sensitive path
    assert risk_score(_diff("app/views.py", added=200)) > base  # more lines changed
    assert risk_score(_diff("app/views.py", status="added")) > base  # new file
    assert risk_score(_diff("tests/test_views.py")) < base  # tests last...
    # ...even a huge, sensitive, new test file ranks below a one-line ordinary change
    assert risk_score(_diff("tests/test_auth.py", added=5000, status="added")) < risk_score(_diff("x.py", added=1))


def test_by_risk_orders_and_keeps_ties_stable():
    diffs = [_diff("a.py"), _diff("tests/test_a.py"), _diff("b.py"), _diff("auth.py"), _diff("big.py", added=300)]
    assert [d.path for d in by_risk(diffs, lambda d: d)] == ["auth.py", "big.py", "a.py", "b.py", "tests/test_a.py"]


def test_cap_files_drops_lowest_risk_and_keeps_order():
    diffs = [_diff("tests/test_a.py"), _diff("a.py"), _diff("auth.py"), _diff("docs.py", added=1)]
    kept, dropped = cap_files(diffs, 2)
    assert [d.path for d in kept] == ["a.py", "auth.py"]  # original order
    assert [d.path for d in dropped] == ["docs.py", "tests/test_a.py"]  # riskiest first


def test_cap_files_ignores_deleted_and_hunkless():
    gone = FileDiff(path="old.py", status="deleted")
    binary = FileDiff(path="logo.png", binary=True)
    diffs = [gone, _diff("a.py"), binary, _diff("b.py")]
    kept, dropped = cap_files(diffs, 2)
    assert kept == diffs and dropped == []


def test_cap_files_under_limit_is_identity():
    diffs = [_diff("a.py"), _diff("b.py")]
    assert cap_files(diffs, 50) == (diffs, [])


def _patch(*paths: str) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,2 @@\n a = 1\n+b = 2\n" for p in paths
    )


async def test_build_context_keeps_the_riskiest_files(settings):
    paths = ["tests/test_x.py", "app/views.py", "app/auth.py"]
    src = InMemorySource({p: "a = 1\nb = 2\n" for p in paths})
    ctx = await build_context(src, parse_unified_diff(_patch(*paths)), settings.model_copy(update={"max_files": 2}))
    assert [fc.path for fc in ctx] == ["app/views.py", "app/auth.py"]


def _git(repo, *args):
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def test_local_large_pr_reports_unreviewed_files(tmp_path, settings):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "tests").mkdir()
    for name in ("tests/test_app.py", "app.py", "auth.py"):
        (tmp_path / name).write_text("a = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "change")

    pr, merged = await context_from_local(tmp_path, "HEAD~1", "HEAD", settings.model_copy(update={"max_files": 2}))
    assert sorted(fc.path for fc in pr.files) == ["app.py", "auth.py"]
    assert pr.skipped_max_files == ["tests/test_app.py"]

    result = await review(pr, FakeLLM({}), merged)
    assert result.skipped_max_files == ["tests/test_app.py"]
    assert result.stats["skipped_max_files"] == 1 and result.stats["files"] == 2
    assert "Large PR" in format_summary(result) and "`tests/test_app.py`" in format_summary(result)


def _file(path: str, lines: int = 20) -> FileContext:
    added = [f"+x = {i}" for i in range(lines)]
    hunk = Hunk(header=f"@@ -0,0 +1,{lines} @@", new_start=1, lines=added, added_lines=list(range(1, lines + 1)))
    content = "\n".join(line[1:] for line in added) + "\n"
    return FileContext(diff=FileDiff(path=path, hunks=[hunk]), content=content)


async def test_budget_drops_lowest_risk_files_first(settings):
    files = [_file("auth/login.py"), _file("tests/test_login.py"), _file("app/views.py"), _file("app/util.py", 2)]
    s = settings.model_copy(update={"reviewer_model": "m", "model_prices": {"m": ModelPrice(input=1.0, output=5.0)}})
    pr = PRContext(repo="t/t", files=files)
    from reviewer.agents.specialists import build_specialists

    budget = estimate_cost(pr, [files[0], files[2]], build_specialists(s), s) * 1.01
    llm = FakeLLM({})
    result = await review(pr, llm, s.model_copy(update={"max_cost_usd": budget}))
    assert result.skipped_budget == ["tests/test_login.py", "app/util.py"]
    reviewed = " ".join(str(u) for u, t in zip(llm.users, llm.calls) if t == "report_findings")
    assert "auth/login.py" in reviewed and "app/views.py" in reviewed
    assert reviewed.index("auth/login.py") < reviewed.index("app/views.py")  # kept files stay in diff order


def test_reports_name_unreviewed_files():
    result = ReviewResult(skipped_max_files=["b.py", "tests/test_b.py"], skipped_budget=["c.py"])
    for text in (format_summary(result), format_markdown(result)):
        assert "Large PR" in text and "`tests/test_b.py`" in text and "Cost budget" in text
    assert [n[0] for n in unreviewed_notes(result)] == ["📚", "💰"]
    assert "large PR (MAX_FILES), not reviewed: b.py, tests/test_b.py" in format_text(result)
    assert max_files_note(ReviewResult()) == "" and "Large PR" not in format_summary(ReviewResult())


def test_max_files_note_truncates_long_lists():
    note = max_files_note(ReviewResult(skipped_max_files=[f"f{i}.py" for i in range(23)]))
    assert note.startswith("📚 **Large PR:** 23 files") and "and 3 more" in note
