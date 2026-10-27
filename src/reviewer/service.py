"""Entry points shared by the CLI, the webhook server, and the GitHub Action."""

from __future__ import annotations

import logging
from pathlib import Path

from reviewer.agents.llm import LLM, AnthropicLLM
from reviewer.config import Settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff, restrict_to_changes
from reviewer.ingest.github import GitHubClient
from reviewer.ingest.noise import split_noise
from reviewer.ingest.sources import GitHubSource, LocalGitSource
from reviewer.models import PRContext, ReviewResult
from reviewer.output.github_review import bot_commented_lines, post_review
from reviewer.pipeline import review
from reviewer.repo_config import apply_repo_config, load_repo_config

log = logging.getLogger(__name__)


def make_llm(settings: Settings) -> LLM:
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    return AnthropicLLM(settings.anthropic_api_key, settings.llm_concurrency)


async def context_from_github(
    client: GitHubClient, repo: str, number: int, settings: Settings, since_sha: str | None = None
) -> tuple[PRContext, Settings]:
    """Returns the PR context and the settings with the repo's `.acr.yml` merged in.

    With `since_sha`, only PR hunks touched by `since_sha...head` are reviewed. If GitHub can't compare
    (the old head vanished in a force-push), we fall back to a full review rather than skip anything."""
    pr = await client.get_pr(repo, number)
    diffs = parse_unified_diff(await client.get_pr_diff(repo, number))
    head = pr["head"]
    if since_sha and since_sha != head["sha"]:
        compare = await client.get_compare_diff(repo, since_sha, head["sha"])
        if compare is None:
            log.info("cannot compare %s...%s on %s#%s; doing a full review", since_sha, head["sha"], repo, number)
            since_sha = None
        else:
            diffs = restrict_to_changes(diffs, parse_unified_diff(compare))
    head_repo = (head.get("repo") or {}).get("full_name") or repo  # forks
    # Config comes from the BASE branch: a PR must not be able to switch off its own review.
    base_sha = (pr.get("base") or {}).get("sha")
    if base_sha:
        settings = apply_repo_config(settings, await load_repo_config(GitHubSource(client, repo, base_sha)))
    source = GitHubSource(client, head_repo, head["sha"])
    diffs, skipped = split_noise(diffs, settings.ignore)
    ctx = PRContext(
        repo=repo,
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        head_sha=head["sha"],
        since_sha=since_sha,
        files=await build_context(source, diffs, settings),
        skipped=skipped,
    )
    return ctx, settings


async def context_from_local(
    repo: str | Path, base: str, head: str, settings: Settings
) -> tuple[PRContext, Settings]:
    """Local runs are trusted, so `.acr.yml` is read from the head ref (commit it to try changes)."""
    source = LocalGitSource(repo, base, head)
    settings = apply_repo_config(settings, await load_repo_config(source))
    diffs, skipped = split_noise(parse_unified_diff(source.diff()), settings.ignore)
    ctx = PRContext(
        repo=str(source.repo),
        title=f"{head} vs {base}",
        files=await build_context(source, diffs, settings),
        skipped=skipped,
    )
    return ctx, settings


async def review_github_pr(
    repo: str, number: int, settings: Settings, post: bool = False, since_sha: str | None = None
) -> ReviewResult:
    if not settings.github_token:
        raise SystemExit("GITHUB_TOKEN is not set")
    async with GitHubClient(settings.github_token) as gh:
        pr, settings = await context_from_github(gh, repo, number, settings, since_sha)
        seen = bot_commented_lines(await gh.list_review_comments(repo, number))
        result = await review(pr, make_llm(settings), settings, already_commented=seen)
        # An incremental pass with nothing new to say stays silent instead of re-posting "no issues".
        if post and not (pr.since_sha and not (result.inline or result.summary_only)):
            await post_review(gh, pr, result)
    return result


async def review_local(repo: str | Path, base: str, head: str, settings: Settings) -> ReviewResult:
    pr, settings = await context_from_local(repo, base, head, settings)
    return await review(pr, make_llm(settings), settings)
