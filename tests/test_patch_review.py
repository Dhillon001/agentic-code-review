"""`acr diff`: review any unified diff with file contents read from a directory."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
from conftest import FakeLLM

from reviewer import cli, service
from reviewer.gate import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import FilesystemSource
from reviewer.models import ReviewResult

PLAIN_DIFF = """\
--- app/pagination.py.orig\t2026-11-13 10:00:00.000000000 -0800
+++ app/pagination.py\t2026-11-13 10:05:00.000000000 -0800
@@ -1,3 +1,3 @@
 def page(items, n, size):
-    start = (n - 1) * size
+    start = n * size
     return items[start:start + size]
--- /dev/null
+++ app/new.py
@@ -0,0 +1,2 @@
+def helper():
+    return page([], 1, 10)
--- app/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
Only in app: README
"""

FORMAT_PATCH = """\
From 1234567890abcdef Mon Sep 17 00:00:00 2001
From: Dev <dev@example.com>
Subject: [PATCH] Tweak paging

---
 app/pagination.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)

diff --git a/app/pagination.py b/app/pagination.py
index 111..222 100644
--- a/app/pagination.py
+++ b/app/pagination.py
@@ -1,3 +1,3 @@
 def page(items, n, size):
-    start = (n - 1) * size
+    start = n * size
     return items[start:start + size]
--
2.43.0
"""

PAGINATION = "def page(items, n, size):\n    start = n * size\n    return items[start:start + size]\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pagination.py").write_text(PAGINATION)
    (tmp_path / "app" / "new.py").write_text("def helper():\n    return page([], 1, 10)\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "page.py").write_text("page = 1\n")  # never searched
    return tmp_path


# ---------------------------------------------------------------- parsing non-git diffs


def test_plain_diff_u_files_status_and_lines():
    files = parse_unified_diff(PLAIN_DIFF)
    assert [(f.path, f.status) for f in files] == [
        ("app/pagination.py", "modified"),
        ("app/new.py", "added"),
        ("app/gone.py", "deleted"),
    ]
    assert files[0].old_path == "app/pagination.py.orig"  # timestamp after the tab is dropped
    assert files[0].added_lines == {2}
    assert files[1].added_lines == {1, 2}
    # "Only in" trailer after the last complete hunk is not a diff line
    assert files[2].hunks[0].lines == ["-x = 1"]


@pytest.mark.parametrize("sig", ["-- ", "--"])  # editors often strip the trailing space
def test_format_patch_signature_not_read_as_diff_lines(sig):
    [fd] = parse_unified_diff(FORMAT_PATCH.replace("\n--\n", f"\n{sig}\n"))
    assert fd.path == "app/pagination.py"
    assert fd.hunks[0].lines[-1] == "     return items[start:start + size]"
    assert fd.commentable_lines == {1, 2, 3}


def test_removed_and_added_lines_that_look_like_headers_stay_in_hunk():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n--- old\n+++ new\n ctx\n"
    [fd] = parse_unified_diff(diff)
    assert fd.hunks[0].lines == ["--- old", "+++ new", " ctx"]
    assert fd.added_lines == {1}


def test_undercounted_hunk_keeps_trailing_diff_lines():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n+c\n"
    [fd] = parse_unified_diff(diff)
    assert fd.added_lines == {1, 2}


# ---------------------------------------------------------------- filesystem source


async def test_filesystem_source_reads_searches_and_lists(tree):
    src = FilesystemSource(tree)
    assert await src.read_file("app/pagination.py") == PAGINATION
    assert await src.read_file("app/missing.py") is None
    assert await src.search("page") == ["app/new.py", "app/pagination.py"]  # .git skipped
    assert await src.list_files() == ["app/new.py", "app/pagination.py"]


async def test_filesystem_source_refuses_paths_outside_root(tree):
    (tree.parent / "secret.txt").write_text("hunter2")
    src = FilesystemSource(tree / "app")
    assert await src.read_file("../secret.txt") is None
    assert await src.read_file("/etc/passwd") is None


async def test_filesystem_source_skips_binary(tree):
    (tree / "app" / "blob.bin").write_bytes(b"page\0\x01")
    src = FilesystemSource(tree)
    assert await src.read_file("app/blob.bin") is None
    assert "app/blob.bin" not in await src.search("page")


# ---------------------------------------------------------------- review end to end


REAL = {"file": "app/pagination.py", "line": 2, "severity": "high", "category": "off-by-one",
        "title": "pages are 1-indexed", "detail": "page 1 now skips the first page", "confidence": 0.9}


async def test_review_patch_end_to_end(tree, settings, monkeypatch):
    llm = FakeLLM({"correctness": [REAL]})
    monkeypatch.setattr(service, "make_llm", lambda s: llm)
    result = await service.review_patch(PLAIN_DIFF, tree, settings, title="my.patch")
    assert [(f.file, f.line) for f in result.inline] == [("app/pagination.py", 2)]
    # the specialist saw the file content read from disk
    assert "start = n * size" in "".join(map(str, llm.users))


async def test_context_from_patch_reads_acr_yml_from_root(tree, settings):
    (tree / ".acr.yml").write_text("ignore: ['app/new.py']\nrules: ['Pages are 1-indexed']\n")
    ctx, merged = await service.context_from_patch(PLAIN_DIFF, tree, settings)
    assert "app/new.py" in ctx.skipped
    assert "Pages are 1-indexed" in merged.rules
    assert ctx.title == "patch"


async def test_warns_when_patch_not_applied_to_root(tmp_path, settings, caplog):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pagination.py").write_text(PAGINATION.replace("n * size", "(n - 1) * size"))
    with caplog.at_level(logging.WARNING, logger="reviewer.service"):
        await service.context_from_patch(FORMAT_PATCH, tmp_path, settings)
    assert "app/pagination.py" in caplog.text and "not applied" in caplog.text


async def test_no_warning_when_applied(tree, settings, caplog):
    with caplog.at_level(logging.WARNING, logger="reviewer.service"):
        await service.context_from_patch(FORMAT_PATCH, tree, settings)
    assert caplog.text == ""


# ---------------------------------------------------------------- CLI wiring


def _patch_cli(monkeypatch, seen: dict, result: ReviewResult | None = None):
    async def fake_review_patch(patch, root, settings, title="patch"):
        seen.update(patch=patch, root=root, title=title)
        return result or ReviewResult()

    monkeypatch.setattr(cli, "review_patch", fake_review_patch)


def test_cli_diff_from_file(monkeypatch, tmp_path, capsys):
    seen: dict = {}
    _patch_cli(monkeypatch, seen)
    p = tmp_path / "change.patch"
    p.write_text(PLAIN_DIFF)
    assert cli.run(["diff", str(p), "--root", str(tmp_path)]) == EXIT_OK
    assert seen == {"patch": PLAIN_DIFF, "root": str(tmp_path), "title": f"patch {p}"}
    assert "No issues" in capsys.readouterr().out


def test_cli_diff_from_stdin_with_gate(monkeypatch, capsys):
    from reviewer.models import Finding, Severity

    finding = Finding(file="app/pagination.py", line=2, severity=Severity.HIGH, category="off-by-one",
                      title="t", detail="d", agent="correctness")
    seen: dict = {}
    _patch_cli(monkeypatch, seen, ReviewResult(inline=[finding]))

    class Stdin:
        buffer = io.BytesIO(PLAIN_DIFF.encode())

    monkeypatch.setattr(cli.sys, "stdin", Stdin)
    assert cli.run(["diff", "-", "--fail-on", "high", "--format", "json"]) == EXIT_FINDINGS
    assert seen["root"] == "." and seen["title"] == "patch from stdin"
    assert '"inline"' in capsys.readouterr().out


def test_cli_diff_missing_file_is_error(capsys):
    assert cli.run(["diff", "/nonexistent/x.patch"]) == EXIT_ERROR
    assert "x.patch" in capsys.readouterr().err
