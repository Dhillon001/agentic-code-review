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


def make_llm(settings: Settings) -> LLM:
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    return AnthropicLLM(settings.anthropic_api_key, settings.llm_concurrency)


async def context_from_github(client: GitHubClient, repo: str, number: int, settings: Settings) -> PRContext:
    pr = await client.get_pr(repo, number)
    diffs = parse_unified_diff(await client.get_pr_diff(repo, number))
    head = pr["head"]
    head_repo = (head.get("repo") or {}).get("full_name") or repo  # forks
    source = GitHubSource(client, head_repo, head["sha"])
    return PRContext(
        repo=repo,
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        head_sha=head["sha"],
        files=await build_context(source, diffs, settings),
    )


async def context_from_local(repo: str | Path, base: str, head: str, settings: Settings) -> PRContext:
    source = LocalGitSource(repo, base, head)
    diffs = parse_unified_diff(source.diff())
    return PRContext(
        repo=str(source.repo),
        title=f"{head} vs {base}",
        files=await build_context(source, diffs, settings),
    )


async def review_github_pr(repo: str, number: int, settings: Settings, post: bool = False) -> ReviewResult:
    if not settings.github_token:
        raise SystemExit("GITHUB_TOKEN is not set")
    async with GitHubClient(settings.github_token) as gh:
        pr = await context_from_github(gh, repo, number, settings)
        result = await review(pr, make_llm(settings), settings)
        if post:
            await post_review(gh, pr, result)
    return result


async def review_local(repo: str | Path, base: str, head: str, settings: Settings) -> ReviewResult:
    pr = await context_from_local(repo, base, head, settings)
    return await review(pr, make_llm(settings), settings)
