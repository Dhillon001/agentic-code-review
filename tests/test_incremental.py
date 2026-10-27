"""Incremental re-review: only new commits' hunks, and never repeat a comment we already posted."""

from __future__ import annotations

import json

import httpx
from conftest import FakeLLM

import reviewer.service as service
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff, restrict_to_changes
from reviewer.ingest.github import GitHubClient
from reviewer.ingest.sources import InMemorySource
from reviewer.models import Finding, PRContext, Severity
from reviewer.output.github_review import bot_commented_lines, format_comment
from reviewer.pipeline import review

A_LINES = [f"x{i} = {i}" for i in range(1, 51)]
A_FILE = "\n".join(A_LINES) + "\n"

# Full PR diff: two hunks in app/a.py (around lines 3 and 42) plus app/b.py.
PR_DIFF = """\
diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -1,5 +1,5 @@
 x1 = 1
 x2 = 2
-x3 = 0
+x3 = 3
 x4 = 4
 x5 = 5
@@ -40,5 +40,5 @@
 x40 = 40
 x41 = 41
-x42 = 0
+x42 = 42
 x43 = 43
 x44 = 44
diff --git a/app/b.py b/app/b.py
--- a/app/b.py
+++ b/app/b.py
@@ -1,2 +1,2 @@
-y = 0
+y = 1
 z = 2
"""

# What the newest push changed: only the second hunk of app/a.py.
COMPARE_DIFF = """\
diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -41,3 +41,3 @@
 x41 = 41
-x42 = 999
+x42 = 42
 x43 = 43
"""


def _finding(file: str, line: int, **kw) -> dict:
    return {"file": file, "line": line, "severity": "high", "category": "bug", "title": "t",
            "detail": "d", "confidence": 0.9, **kw}


# ---------------------------------------------------------------- pure helpers


def test_restrict_keeps_only_overlapping_hunks():
    out = restrict_to_changes(parse_unified_diff(PR_DIFF), parse_unified_diff(COMPARE_DIFF))
    assert [fd.path for fd in out] == ["app/a.py"]
    assert [h.new_start for h in out[0].hunks] == [40]
    assert 42 in out[0].commentable_lines and 3 not in out[0].commentable_lines


def test_restrict_counts_pure_deletions_as_touching_their_spot():
    deletion = """\
diff --git a/app/b.py b/app/b.py
--- a/app/b.py
+++ b/app/b.py
@@ -2,1 +1,0 @@
-gone = 1
"""
    out = restrict_to_changes(parse_unified_diff(PR_DIFF), parse_unified_diff(deletion))
    assert [fd.path for fd in out] == ["app/b.py"]


def test_bot_commented_lines_only_counts_our_live_comments():
    ours = format_comment(Finding(agent="security", file="app/a.py", line=42, severity=Severity.HIGH,
                                  category="bug", title="t", detail="d"))
    comments = [
        {"path": "app/a.py", "line": 42, "body": ours},
        {"path": "app/a.py", "line": 7, "body": "human says hi"},
        {"path": "app/a.py", "line": None, "original_line": 3, "body": ours},  # outdated
    ]
    assert bot_commented_lines(comments) == {("app/a.py", 42)}


async def test_pipeline_skips_already_commented_before_verifying(settings):
    files = await build_context(InMemorySource({"app/a.py": A_FILE}), parse_unified_diff(PR_DIFF)[:1], settings)
    pr = PRContext(repo="o/r", files=files)
    llm = FakeLLM({"correctness": [_finding("app/a.py", 42)]})
    result = await review(pr, llm, settings, already_commented={("app/a.py", 42)})
    assert result.inline == [] and result.summary_only == []
    assert result.stats["already_commented"] == 1
    assert "report_verdicts" not in llm.calls


async def test_pipeline_drops_finding_snapped_onto_old_comment(settings):
    files = await build_context(InMemorySource({"app/a.py": A_FILE}), parse_unified_diff(PR_DIFF)[:1], settings)
    pr = PRContext(repo="o/r", files=files)
    llm = FakeLLM({"correctness": [_finding("app/a.py", 46)]})  # off-diff, snaps to 44
    result = await review(pr, llm, settings, already_commented={("app/a.py", 44)})
    assert result.inline == [] and result.stats["already_commented"] == 1


# ---------------------------------------------------------------- GitHub plumbing (mock transport)


class FakeGitHub:
    """Minimal GitHub REST stand-in. Records every request path; `posted` collects review payloads."""

    def __init__(self, compare: str | None = COMPARE_DIFF, comments: list[dict] | None = None):
        self.compare = compare
        self.comments = comments or []
        self.paths: list[str] = []
        self.posted: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.paths.append(path)
        accept = req.headers.get("accept", "")
        if path == "/repos/o/r/pulls/7" and "diff" in accept:
            return httpx.Response(200, text=PR_DIFF)
        if path == "/repos/o/r/pulls/7":
            return httpx.Response(200, json={"title": "t", "body": "", "head": {"sha": "new"},
                                             "base": {"sha": "base"}})
        if path == "/repos/o/r/compare/old...new":
            return httpx.Response(200, text=self.compare) if self.compare else httpx.Response(404)
        if path == "/repos/o/r/pulls/7/comments":
            page = int(req.url.params["page"])
            return httpx.Response(200, json=self.comments[(page - 1) * 100 : page * 100])
        if path == "/repos/o/r/pulls/7/reviews":
            self.posted.append(json.loads(req.content))
            return httpx.Response(200, json={"id": 1})
        if path == "/repos/o/r/contents/app/a.py":
            return httpx.Response(200, text=A_FILE)
        if path == "/repos/o/r/contents/app/b.py":
            return httpx.Response(200, text="y = 1\nz = 2\n")
        if path == "/search/code":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404)

    def client(self) -> GitHubClient:
        return GitHubClient("t", transport=httpx.MockTransport(self.handler))


async def test_list_review_comments_paginates():
    gh = FakeGitHub(comments=[{"id": i, "path": "p", "line": i, "body": ""} for i in range(150)])
    async with gh.client() as c:
        got = await c.list_review_comments("o/r", 7)
    assert len(got) == 150
    assert gh.paths.count("/repos/o/r/pulls/7/comments") == 2


async def test_context_since_reviews_only_new_hunks(settings):
    gh = FakeGitHub()
    async with gh.client() as c:
        ctx, _ = await service.context_from_github(c, "o/r", 7, settings, since_sha="old")
    assert ctx.since_sha == "old"
    assert [(fc.path, [h.new_start for h in fc.diff.hunks]) for fc in ctx.files] == [("app/a.py", [40])]


async def test_context_falls_back_to_full_review_when_compare_fails(settings):
    gh = FakeGitHub(compare=None)  # e.g. old head force-pushed away
    async with gh.client() as c:
        ctx, _ = await service.context_from_github(c, "o/r", 7, settings, since_sha="old")
    assert ctx.since_sha is None
    assert sorted(fc.path for fc in ctx.files) == ["app/a.py", "app/b.py"]


def _patch_service(monkeypatch, gh: FakeGitHub, llm: FakeLLM) -> None:
    monkeypatch.setattr(service, "GitHubClient", lambda token: gh.client())
    monkeypatch.setattr(service, "make_llm", lambda s: llm)


async def test_rereview_posts_only_new_findings(settings, monkeypatch):
    old = format_comment(Finding(agent="correctness", file="app/a.py", line=42, severity=Severity.HIGH,
                                 category="bug", title="t", detail="d"))
    gh = FakeGitHub(comments=[{"path": "app/a.py", "line": 42, "body": old}])
    llm = FakeLLM({"correctness": [_finding("app/a.py", 42), _finding("app/b.py", 1, category="other")]})
    _patch_service(monkeypatch, gh, llm)
    await service.review_github_pr("o/r", 7, settings, post=True)
    [posted] = gh.posted
    assert [(c["path"], c["line"]) for c in posted["comments"]] == [("app/b.py", 1)]


async def test_incremental_with_nothing_new_stays_silent(settings, monkeypatch):
    gh = FakeGitHub()
    _patch_service(monkeypatch, gh, FakeLLM({}))
    result = await service.review_github_pr("o/r", 7, settings, post=True, since_sha="old")
    assert result.stats["files"] == 1
    assert gh.posted == []
