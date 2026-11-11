"""Orchestration: secret pre-pass + specialists x file-batches in parallel -> dedupe -> verify -> rank -> place."""

from __future__ import annotations

import asyncio
import logging
import time

from reviewer.agents.llm import LLM
from reviewer.agents.specialists import (
    Specialist,
    build_review_prompt,
    build_specialists,
    render_files,
    run_specialist,
)
from reviewer.agents.verifier import verify
from reviewer.config import Settings
from reviewer.models import FileContext, Finding, PRContext, ReviewResult
from reviewer.ranking import dedupe, place, rank
from reviewer.secret_scan import scan as scan_secrets
from reviewer.secret_scan import scrub
from reviewer.suppress import split_suppressed, suppressions_for
from reviewer.usage import recording, summarize

log = logging.getLogger(__name__)


def batch_files(files: list[FileContext], settings: Settings) -> list[list[FileContext]]:
    """Greedy-pack files into batches that fit the prompt budget. Oversized files go solo."""
    batches: list[list[FileContext]] = []
    cur: list[FileContext] = []
    size = 0
    for fc in files:
        n = len(render_files([fc], settings))
        if cur and size + n > settings.max_prompt_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(fc)
        size += n
    if cur:
        batches.append(cur)
    return batches


async def _review_batch(
    llm: LLM, specialists: list[Specialist], pr: PRContext, batch: list[FileContext], settings: Settings
) -> list[list[Finding] | BaseException]:
    """Every specialist reviews one batch. The PR context is rendered once and sent byte-identical to
    each of them, so after the first call the rest read it from the prompt cache."""
    shared = build_review_prompt(pr, batch, settings)

    async def one(spec: Specialist) -> list[Finding]:
        return await run_specialist(llm, spec, pr, batch, settings, shared_context=shared)

    head: list[list[Finding] | BaseException] = []
    rest = specialists
    if settings.cache_warmup and len(specialists) > 1:
        head = list(await asyncio.gather(one(specialists[0]), return_exceptions=True))
        rest = specialists[1:]
    return head + list(await asyncio.gather(*(one(s) for s in rest), return_exceptions=True))


async def review(
    pr: PRContext,
    llm: LLM,
    settings: Settings,
    specialists: list[Specialist] | None = None,
    already_commented: set[tuple[str, int]] | None = None,
) -> ReviewResult:
    """`already_commented`: (file, line) pairs we posted on in an earlier review; findings there are
    dropped before verification so a re-review never repeats itself (and never pays to re-verify).

    Every model call made during the review is tallied into `ReviewResult.usage`."""
    t0 = time.perf_counter()
    with recording() as calls:
        result = await _review(pr, llm, settings, specialists, already_commented)
    result.usage = summarize(calls, settings.model_prices, time.perf_counter() - t0)
    return result


async def _review(
    pr: PRContext,
    llm: LLM,
    settings: Settings,
    specialists: list[Specialist] | None,
    already_commented: set[tuple[str, int]] | None,
) -> ReviewResult:
    if specialists is None:
        specialists = build_specialists(settings)  # built-ins + .acr.yml `agents:` - `disable:`
    reviewable = [fc for fc in pr.files if fc.diff.status != "deleted" and fc.diff.hunks]
    if not reviewable:
        return ReviewResult(stats={"files": 0, "skipped": len(pr.skipped)})

    # deterministic pre-pass: no model call, and its hits skip the verifier
    secrets, secret_values = scan_secrets(reviewable) if settings.secret_scan else ([], set())
    deterministic = {id(f) for f in secrets}

    batches = batch_files(reviewable, settings)
    per_batch = await asyncio.gather(*(_review_batch(llm, specialists, pr, b, settings) for b in batches))
    results = [r for batch_results in per_batch for r in batch_results]

    raw = []
    errors = 0
    for r in results:
        if isinstance(r, BaseException):
            errors += 1
            log.warning("specialist call failed: %r", r)
        else:
            raw.extend(r)

    # secrets go first so they win ties in dedupe (an agent flagging the same line is a duplicate)
    candidates = dedupe([f for f in [*secrets, *raw] if f.confidence >= settings.min_confidence])
    seen = already_commented or set()
    repeats = [f for f in candidates if (f.file, f.line) in seen]
    candidates = [f for f in candidates if (f.file, f.line) not in seen]
    files_by_path = {fc.path: fc for fc in reviewable}
    # `acr: ignore` comments: drop before verifying (no point paying to confirm a silenced finding)
    suppressions = suppressions_for(reviewable)
    candidates, suppressed = split_suppressed(candidates, suppressions)

    to_verify = [f for f in candidates if id(f) not in deterministic]
    if settings.verify and to_verify:
        confirmed, rejected = await verify(llm, to_verify, files_by_path, settings)
    else:
        confirmed, rejected = to_verify, []
    confirmed = [f for f in candidates if id(f) in deterministic] + confirmed
    # an agent's own severity floor holds even if the verifier downgraded one of its findings
    by_name = {s.name: s for s in specialists}
    confirmed = [f for f in confirmed if f.agent not in by_name or by_name[f.agent].keeps(f)]

    ranked = rank(confirmed, settings.min_severity, settings.max_comments)
    inline, summary_only = place(ranked, files_by_path)
    snapped_repeats = [f for f in inline if (f.file, f.line) in seen]  # snapped onto an old comment
    inline = [f for f in inline if (f.file, f.line) not in seen]
    # ...and again after snapping, in case a finding was moved onto a suppressed line
    inline, snapped_suppressed = split_suppressed(inline, suppressions)

    return ReviewResult(
        inline=scrub(inline, secret_values),
        summary_only=scrub(summary_only, secret_values),
        rejected=scrub(rejected, secret_values),
        stats={
            "files": len(reviewable),
            "skipped": len(pr.skipped),
            "batches": len(batches),
            "agent_calls": len(results),
            "agent_errors": errors,
            "secrets": len(secrets),
            "raw_findings": len(raw),
            "already_commented": len(repeats) + len(snapped_repeats),
            "candidates": len(candidates),
            "confirmed": len(confirmed),
            "rejected": len(rejected),
            "suppressed": len(suppressed) + len(snapped_suppressed),
            "posted": len(inline) + len(summary_only),
        },
    )
