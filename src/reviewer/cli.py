"""
acr pr owner/repo 123 [--post] [--since SHA] [--json]
                                            review a GitHub PR (optionally post; --since = only new commits)
acr local [--repo .] [--base main]          review your current branch against a base
acr feedback owner/repo [--days 30]         👍/👎 precision of the bot's comments, per agent and category
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from reviewer import __version__
from reviewer.config import get_settings
from reviewer.feedback import format_report
from reviewer.models import ReviewResult
from reviewer.output.github_review import ICON
from reviewer.service import feedback_report, review_github_pr, review_local
from reviewer.usage import format_usage


def print_result(result: ReviewResult) -> None:
    findings = result.inline + result.summary_only
    if not findings:
        print("✅ No issues survived verification.")
    for f in findings:
        print(f"{ICON[f.severity.value]} [{f.severity.value}] {f.file}:{f.line}  ({f.agent}/{f.category})")
        print(f"   {f.title}")
        print(f"   {f.detail}")
        if f.suggestion:
            print(f"   fix: {f.suggestion}")
        if f.single_line_replacement is not None:
            print(f"   replace line {f.line} with: {f.single_line_replacement}")
        print()
    print("stats:", result.stats)
    print("usage:", format_usage(result.usage))


def main() -> None:
    p = argparse.ArgumentParser(prog="acr", description="Agentic code review")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("pr", help="review a GitHub pull request")
    pr.add_argument("repo", help="owner/repo")
    pr.add_argument("number", type=int)
    pr.add_argument("--post", action="store_true", help="post the review to GitHub")
    pr.add_argument("--since", metavar="SHA", help="only review hunks changed since this commit")
    pr.add_argument("--json", action="store_true")

    lo = sub.add_parser("local", help="review a local branch")
    lo.add_argument("--repo", default=".")
    lo.add_argument("--base", default="main")
    lo.add_argument("--head", default="HEAD")
    lo.add_argument("--json", action="store_true")

    fb = sub.add_parser("feedback", help="precision of posted comments from 👍/👎 reactions")
    fb.add_argument("repo", help="owner/repo")
    fb.add_argument("--days", type=int, default=30, help="look back this many days (0 = all time)")
    fb.add_argument("--json", action="store_true")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    settings = get_settings()

    if args.cmd == "feedback":
        report = asyncio.run(feedback_report(args.repo, settings, args.days))
        print(report.model_dump_json(indent=2) if args.json else format_report(report))
        return

    if args.cmd == "pr":
        result = asyncio.run(
            review_github_pr(args.repo, args.number, settings, post=args.post, since_sha=args.since or None)
        )
    else:
        result = asyncio.run(review_local(args.repo, args.base, args.head, settings))

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print_result(result)


if __name__ == "__main__":
    main()
