"""Feedback report: how often do humans 👍 vs 👎 the bot's inline comments, per agent and per category?

Everything here except `fetch_feedback` is a pure function over GitHub's review-comments payload, so the
aggregation is tested with fixture JSON and no network."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, computed_field

from reviewer.ingest.github import GitHubClient
from reviewer.output.github_review import FOOTER

# Matches what `format_comment` writes: "<icon> **High · category** — title" ... "<sub>agent: name · confidence 80%".
_HEADER = re.compile(r"\*\*(?P<severity>[A-Za-z]+) · (?P<category>.+?)\*\* — ")
_FOOTER = re.compile(re.escape(FOOTER) + r"(?P<agent>.+?) · confidence ")


class CommentFeedback(BaseModel):
    agent: str
    category: str
    severity: str = ""
    up: int = 0
    down: int = 0
    path: str = ""
    url: str = ""
    created_at: datetime | None = None


class Bucket(BaseModel):
    name: str
    comments: int = 0
    up: int = 0
    down: int = 0
    rated: int = 0  # comments with at least one 👍 or 👎
    useful: int = 0  # rated comments with more 👍 than 👎

    @computed_field  # type: ignore[prop-decorator]
    @property
    def precision(self) -> float | None:
        """Share of rated comments humans called useful. A comment counts as useful when 👍 outnumber 👎."""
        return self.useful / self.rated if self.rated else None


class FeedbackReport(BaseModel):
    repo: str
    days: int
    overall: Bucket
    by_agent: list[Bucket] = Field(default_factory=list)
    by_category: list[Bucket] = Field(default_factory=list)


def parse_comment(body: str) -> tuple[str, str, str] | None:
    """(agent, category, severity) from a comment this reviewer posted, or None if it isn't one of ours."""
    if not body or FOOTER not in body:
        return None
    foot = _FOOTER.search(body)
    head = _HEADER.search(body)
    if not foot:
        return None
    category = head["category"].strip() if head else "unknown"
    severity = head["severity"].lower() if head else ""
    return foot["agent"].strip() or "unknown", category, severity


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def extract_feedback(comments: list[dict], since: datetime | None = None) -> list[CommentFeedback]:
    """Our comments (recognised by the footer) created at or after `since`, with their reaction counts.
    Thread replies are skipped (the bot only posts top-level comments), so a human quoting our footer
    in a reply isn't miscounted."""
    out: list[CommentFeedback] = []
    for c in comments:
        if c.get("in_reply_to_id"):
            continue
        parsed = parse_comment(c.get("body") or "")
        if parsed is None:
            continue
        created = _parse_time(c.get("created_at"))
        if since is not None and created is not None and created < since:
            continue
        reactions = c.get("reactions") or {}
        agent, category, severity = parsed
        out.append(
            CommentFeedback(
                agent=agent,
                category=category,
                severity=severity,
                up=int(reactions.get("+1", 0) or 0),
                down=int(reactions.get("-1", 0) or 0),
                path=c.get("path") or "",
                url=c.get("html_url") or "",
                created_at=created,
            )
        )
    return out


def _add(b: Bucket, f: CommentFeedback) -> None:
    b.comments += 1
    b.up += f.up
    b.down += f.down
    if f.up or f.down:
        b.rated += 1
        if f.up > f.down:
            b.useful += 1


def _group(feedback: list[CommentFeedback], key: str) -> list[Bucket]:
    buckets: dict[str, Bucket] = {}
    for f in feedback:
        name = getattr(f, key)
        _add(buckets.setdefault(name, Bucket(name=name)), f)
    # Most-rated first, so the rows with real signal lead; ties broken by name for stable output.
    return sorted(buckets.values(), key=lambda b: (-b.rated, -b.comments, b.name))


def aggregate(feedback: list[CommentFeedback], repo: str = "", days: int = 0) -> FeedbackReport:
    overall = Bucket(name="all")
    for f in feedback:
        _add(overall, f)
    return FeedbackReport(
        repo=repo,
        days=days,
        overall=overall,
        by_agent=_group(feedback, "agent"),
        by_category=_group(feedback, "category"),
    )


def _fmt_precision(b: Bucket) -> str:
    return "—" if b.precision is None else f"{b.precision:.0%}"


def _table(title: str, rows: list[Bucket]) -> list[str]:
    width = max([len(title), *(len(b.name) for b in rows)])
    lines = [f"{title:<{width}}  comments  rated   👍   👎  precision"]
    for b in rows:
        lines.append(
            f"{b.name:<{width}}  {b.comments:>8}  {b.rated:>5}  {b.up:>3}  {b.down:>3}  {_fmt_precision(b):>9}"
        )
    return lines


def format_report(report: FeedbackReport) -> str:
    o = report.overall
    span = f"last {report.days} days" if report.days else "all time"
    if not o.comments:
        return f"No review comments from this bot on {report.repo} ({span})."
    lines = [
        (
            f"Feedback on {report.repo} ({span}): {o.comments} comments, {o.rated} rated, "
            f"precision {_fmt_precision(o)}"
        ),
        "",
        *_table("agent", report.by_agent),
        "",
        *_table("category", report.by_category),
        "",
        "precision = rated comments with more 👍 than 👎 ÷ comments with any 👍/👎",
    ]
    return "\n".join(lines)


async def fetch_feedback(
    client: GitHubClient, repo: str, days: int = 30, now: datetime | None = None
) -> FeedbackReport:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days > 0 else None
    comments = await client.list_repo_review_comments(
        repo, since=since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None
    )
    return aggregate(extract_feedback(comments, since), repo=repo, days=days)
