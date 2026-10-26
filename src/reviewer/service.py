"""Entry points shared by the CLI, the webhook server, and the GitHub Action."""

from __future__ import annotations

from pathlib import Path

from reviewer.agents.llm import LLM, AnthropicLLM
from reviewer.config import Settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.github import GitHubClient
from reviewer.ingest.sources import GitHubSource, LocalGitSource
from reviewer.models import PRContext, ReviewResult
from reviewer.output.github_review import post_review
from reviewer.pipeline import review
from reviewer.repo_config import apply_repo_config, filter_ignored, load_repo_config


def make_llm(settings: Settings) -> LLM:
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    return AnthropicLLM(settings.anthropic_api_key, settings.llm_concurrency)


async def context_from_github(
    client: GitHubClient, repo: str, number: int, settings: Settings
) -> tuple[PRContext, Settings]:
    """Returns the PR context and the settings with the repo's `.acr.yml` merged in."""
    pr = await client.get_pr(repo, number)
    diffs = parse_unified_diff(await client.get_pr_diff(repo, number))
    head = pr["head"]
    head_repo = (head.get("repo") or {}).get("full_name") or repo  # forks
    # Config comes from the BASE branch: a PR must not be able to switch off its own review.
    base_sha = (pr.get("base") or {}).get("sha")
    if base_sha:
        settings = apply_repo_config(settings, await load_repo_config(GitHubSource(client, repo, base_sha)))
    source = GitHubSource(client, head_repo, head["sha"])
    ctx = PRContext(
        repo=repo,
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        head_sha=head["sha"],
        files=await build_context(source, filter_ignored(diffs, settings.ignore), settings),
    )
    return ctx, settings


async def context_from_local(
    repo: str | Path, base: str, head: str, settings: Settings
) -> tuple[PRContext, Settings]:
    """Local runs are trusted, so `.acr.yml` is read from the head ref (commit it to try changes)."""
    source = LocalGitSource(repo, base, head)
    settings = apply_repo_config(settings, await load_repo_config(source))
    diffs = parse_unified_diff(source.diff())
    ctx = PRContext(
        repo=str(source.repo),
        title=f"{head} vs {base}",
        files=await build_context(source, filter_ignored(diffs, settings.ignore), settings),
    )
    return ctx, settings


async def review_github_pr(repo: str, number: int, settings: Settings, post: bool = False) -> ReviewResult:
    if not settings.github_token:
        raise SystemExit("GITHUB_TOKEN is not set")
    async with GitHubClient(settings.github_token) as gh:
        pr, settings = await context_from_github(gh, repo, number, settings)
        result = await review(pr, make_llm(settings), settings)
        if post:
            await post_review(gh, pr, result)
    return result


async def review_local(repo: str | Path, base: str, head: str, settings: Settings) -> ReviewResult:
    pr, settings = await context_from_local(repo, base, head, settings)
    return await review(pr, make_llm(settings), settings)
