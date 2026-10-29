"""Feedback report: parse our comments' footers, count 👍/👎, and aggregate precision per agent and category."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from reviewer.feedback import (
    aggregate,
    extract_feedback,
    fetch_feedback,
    format_report,
    parse_comment,
)
from reviewer.ingest.github import GitHubClient
from reviewer.models import Finding, Severity
from reviewer.output.github_review import format_comment

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "feedback_comments.json").read_text())
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
SINCE = datetime(2026, 9, 29, tzinfo=timezone.utc)  # 30 days before NOW


def _by_name(buckets):
    return {b.name: b for b in buckets}


def test_parse_comment_round_trips_format_comment():
    body = format_comment(Finding(agent="security", file="a.py", line=3, severity=Severity.CRITICAL,
                                  category="path traversal", title="t", detail="d", replacement="x = 1"))
    assert parse_comment(body) == ("security", "path traversal", "critical")


@pytest.mark.parametrize("body", ["", "LGTM", "<sub>not ours</sub>", "**High · bug** — no footer"])
def test_parse_comment_ignores_other_comments(body):
    assert parse_comment(body) is None


def test_extract_skips_human_replies_and_old_comments():
    fb = extract_feedback(FIXTURE, since=SINCE)
    assert len(fb) == 6  # 8 comments: one human reply, one created before the window
    assert all(f.agent != "testing" for f in fb)
    assert fb[0].up == 2 and fb[0].down == 0 and fb[0].url.endswith("r1001")


def test_extract_ignores_replies_quoting_our_footer():
    quoted = {**FIXTURE[0], "id": 3000, "in_reply_to_id": 1001, "body": "> " + FIXTURE[0]["body"]}
    assert len(extract_feedback([quoted])) == 0


def test_extract_without_window_keeps_everything_ours():
    assert len(extract_feedback(FIXTURE)) == 7


def test_aggregate_per_agent_and_category():
    report = aggregate(extract_feedback(FIXTURE, since=SINCE), repo="o/r", days=30)

    o = report.overall
    assert (o.comments, o.rated, o.useful, o.up, o.down) == (6, 5, 3, 7, 2)
    assert o.precision == pytest.approx(3 / 5)

    agents = _by_name(report.by_agent)
    assert agents["correctness"].precision == pytest.approx(2 / 3)
    assert agents["security"].rated == 1 and agents["security"].precision == 0.0  # a 1–1 tie isn't useful
    assert agents["security"].comments == 2  # the unrated one still counts as posted
    assert agents["performance"].precision == 1.0
    assert [b.name for b in report.by_agent] == ["correctness", "security", "performance"]  # most rated, then most posted

    cats = _by_name(report.by_category)
    assert cats["bug"].precision == 0.5
    assert cats["race-condition"].precision == 1.0
    assert cats["n+1-query"].up == 3


def test_unrated_bucket_has_no_precision():
    fb = [f for f in extract_feedback(FIXTURE) if f.up == f.down == 0]
    report = aggregate(fb)
    assert report.overall.precision is None
    assert "—" in format_report(report)
    assert json.loads(report.model_dump_json())["overall"]["precision"] is None


def test_format_report():
    text = format_report(aggregate(extract_feedback(FIXTURE, since=SINCE), repo="o/r", days=30))
    assert text.startswith("Feedback on o/r (last 30 days): 6 comments, 5 rated, precision 60%")
    assert any(line.startswith("correctness") and line.endswith("67%") for line in text.splitlines())
    assert any(line.startswith("n+1-query") and line.endswith("100%") for line in text.splitlines())


def test_format_report_empty():
    assert format_report(aggregate([], repo="o/r", days=7)) == "No review comments from this bot on o/r (last 7 days)."


async def test_fetch_feedback_uses_repo_comments_endpoint():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/repos/o/r/pulls/comments":
            page = int(req.url.params["page"])
            return httpx.Response(200, json=FIXTURE[(page - 1) * 100 : page * 100])
        return httpx.Response(404)

    async with GitHubClient("t", transport=httpx.MockTransport(handler)) as gh:
        report = await fetch_feedback(gh, "o/r", days=30, now=NOW)

    [req] = seen
    assert req.url.params["since"] == "2026-09-29T00:00:00Z"
    assert req.url.params["sort"] == "created" and req.url.params["direction"] == "desc"
    assert report.overall.comments == 6 and report.days == 30


async def test_list_repo_review_comments_paginates():
    pages: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params["page"])
        pages.append(page)
        assert "since" not in req.url.params
        return httpx.Response(200, json=[{"id": i} for i in range(100 if page == 1 else 20)])

    async with GitHubClient("t", transport=httpx.MockTransport(handler)) as gh:
        got = await gh.list_repo_review_comments("o/r")
    assert len(got) == 120 and pages == [1, 2]
