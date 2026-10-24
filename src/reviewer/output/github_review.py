from __future__ import annotations

from collections import Counter

from reviewer.ingest.github import GitHubClient
from reviewer.models import Finding, PRContext, ReviewResult

ICON = {"critical": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡"}


def format_comment(f: Finding) -> str:
    out = f"{ICON[f.severity.value]} **{f.severity.value.title()} · {f.category}** — {f.title}\n\n{f.detail}"
    if f.suggestion:
        out += f"\n\n**Suggested fix:** {f.suggestion}"
    out += f"\n\n<sub>agent: {f.agent} · confidence {f.confidence:.0%}{' · verified' if f.verified else ''}</sub>"
    return out


def format_summary(result: ReviewResult) -> str:
    posted = result.inline + result.summary_only
    if not posted:
        return "🤖 **Agentic review:** no issues found that survived verification."
    counts = Counter(f.severity.value for f in posted)
    tally = " · ".join(f"{ICON[s]} {counts[s]} {s}" for s in ICON if counts[s])
    lines = [f"🤖 **Agentic review:** {tally}"]
    if result.summary_only:
        lines.append("\n**Outside the diff:**")
        for f in result.summary_only:
            lines.append(f"- `{f.file}:{f.line}` {ICON[f.severity.value]} **{f.title}** — {f.detail}")
    s = result.stats
    lines.append(
        f"\n<sub>{s.get('raw_findings', 0)} raw → {s.get('candidates', 0)} candidates → "
        f"{s.get('confirmed', 0)} verified · {s.get('rejected', 0)} rejected by verifier</sub>"
    )
    return "\n".join(lines)


async def post_review(client: GitHubClient, pr: PRContext, result: ReviewResult) -> dict:
    if pr.number is None or not pr.head_sha:
        raise ValueError("PRContext needs number and head_sha to post a review")
    comments = [
        {"path": f.file, "line": f.line, "side": "RIGHT", "body": format_comment(f)} for f in result.inline
    ]
    return await client.post_review(pr.repo, pr.number, pr.head_sha, format_summary(result), comments)
