"""Updatable summary comment: one marked issue comment per PR, edited in place on re-review."""

from __future__ import annotations

import json

import httpx

from reviewer.ingest.github import GitHubClient
from reviewer.models import Finding, PRContext, ReviewResult, Severity
from reviewer.output.github_review import (
    SUMMARY_MARKER,
    find_summary_comment,
    format_summary,
    post_review,
    summary_body,
)


class FakeIssueGitHub:
    """Stand-in for the review + issue-comment endpoints of PR o/r#7. Records (method, path, payload)."""

    def __init__(self, comments: list[dict] | None = None):
        self.comments = list(comments or [])
        self.requests: list[tuple[str, str, dict | None]] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        payload = json.loads(req.content) if req.content else None
        self.requests.append((method, path, payload))
        if path == "/repos/o/r/pulls/7/reviews" and method == "POST":
            return httpx.Response(200, json={"id": 100})
        if path == "/repos/o/r/issues/7/comments" and method == "GET":
            page = int(req.url.params["page"])
            return httpx.Response(200, json=self.comments[(page - 1) * 100 : page * 100])
        if path == "/repos/o/r/issues/7/comments" and method == "POST":
            new = {"id": 1000 + len(self.comments), "body": payload["body"]}
            self.comments.append(new)
            return httpx.Response(201, json=new)
        if path.startswith("/repos/o/r/issues/comments/") and method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = payload["body"]
                    return httpx.Response(200, json=c)
            return httpx.Response(404)
        return httpx.Response(404)

    def client(self) -> GitHubClient:
        return GitHubClient("t", transport=httpx.MockTransport(self.handler))

    def calls(self, method: str) -> list[str]:
        return [p for m, p, _ in self.requests if m == method]


PR = PRContext(repo="o/r", number=7, head_sha="abcdef1234567")


def _result(n_inline: int = 1) -> ReviewResult:
    f = Finding(agent="correctness", file="a.py", line=3, severity=Severity.HIGH, category="bug",
                title="t", detail="d", verified=True)
    return ReviewResult(inline=[f] * n_inline)


def test_find_summary_comment_picks_latest_marked():
    comments = [
        {"id": 1, "body": "lgtm"},
        {"id": 2, "body": f"{SUMMARY_MARKER}\nold"},
        {"id": 3, "body": None},
        {"id": 4, "body": f"{SUMMARY_MARKER}\nnewer"},
    ]
    assert find_summary_comment(comments)["id"] == 4
    assert find_summary_comment([{"id": 1, "body": "🤖 **Agentic review:** no marker"}]) is None


def test_summary_body_has_marker_summary_and_head():
    result = _result()
    body = summary_body(result, PR.head_sha)
    assert body.startswith(SUMMARY_MARKER + "\n")
    assert format_summary(result) in body
    assert body.endswith("`abcdef1`</sub>")


async def test_first_review_creates_summary_comment():
    gh = FakeIssueGitHub(comments=[{"id": 1, "body": "human comment"}])
    async with gh.client() as c:
        out = await post_review(c, PR, _result())
    assert gh.calls("POST") == ["/repos/o/r/pulls/7/reviews", "/repos/o/r/issues/7/comments"]
    assert gh.calls("PATCH") == []
    review = next(p for m, path, p in gh.requests if path.endswith("/reviews"))
    assert SUMMARY_MARKER not in review["body"] and len(review["comments"]) == 1
    assert out["review"]["id"] == 100 and SUMMARY_MARKER in out["summary"]["body"]


async def test_rereview_edits_summary_in_place():
    gh = FakeIssueGitHub()
    async with gh.client() as c:
        await post_review(c, PR, _result())
        await post_review(c, PRContext(repo="o/r", number=7, head_sha="9999999aaaa"), _result(0))
    assert gh.calls("POST").count("/repos/o/r/issues/7/comments") == 1  # never stacks a second summary
    assert gh.calls("PATCH") == ["/repos/o/r/issues/comments/1000"]
    [summary] = gh.comments
    assert "no issues found" in summary["body"] and "`9999999`" in summary["body"]


async def test_no_inline_findings_posts_no_review():
    gh = FakeIssueGitHub()
    async with gh.client() as c:
        out = await post_review(c, PR, _result(0))
    assert "/repos/o/r/pulls/7/reviews" not in gh.calls("POST")
    assert out["review"] is None and len(gh.comments) == 1


async def test_list_issue_comments_paginates():
    gh = FakeIssueGitHub(comments=[{"id": i, "body": ""} for i in range(150)])
    async with gh.client() as c:
        got = await c.list_issue_comments("o/r", 7)
    assert len(got) == 150 and gh.calls("GET").count("/repos/o/r/issues/7/comments") == 2
