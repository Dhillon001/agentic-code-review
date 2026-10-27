"""Orchestration: fan out specialists x file-batches in parallel -> dedupe -> verify -> rank -> place."""

from __future__ import annotations

import asyncio
import logging

from reviewer.agents.llm import LLM
from reviewer.agents.specialists import SPECIALISTS, Specialist, render_files, run_specialist
from reviewer.agents.verifier import verify
from reviewer.config import Settings
from reviewer.models import FileContext, PRContext, ReviewResult
from reviewer.ranking import dedupe, place, rank

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


async def review(
    pr: PRContext,
    llm: LLM,
    settings: Settings,
    specialists: list[Specialist] | None = None,
    already_commented: set[tuple[str, int]] | None = None,
) -> ReviewResult:
    """`already_commented`: (file, line) pairs we posted on in an earlier review; findings there are
    dropped before verification so a re-review never repeats itself (and never pays to re-verify)."""
    specialists = specialists or SPECIALISTS
    reviewable = [fc for fc in pr.files if fc.diff.status != "deleted" and fc.diff.hunks]
    if not reviewable:
        return ReviewResult(stats={"files": 0, "skipped": len(pr.skipped)})

    batches = batch_files(reviewable, settings)
    tasks = [run_specialist(llm, s, pr, b, settings) for s in specialists for b in batches]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    raw = []
    errors = 0
    for r in results:
        if isinstance(r, BaseException):
            errors += 1
            log.warning("specialist call failed: %r", r)
        else:
            raw.extend(r)

    candidates = dedupe([f for f in raw if f.confidence >= settings.min_confidence])
    seen = already_commented or set()
    repeats = [f for f in candidates if (f.file, f.line) in seen]
    candidates = [f for f in candidates if (f.file, f.line) not in seen]
    files_by_path = {fc.path: fc for fc in reviewable}

    if settings.verify and candidates:
        confirmed, rejected = await verify(llm, candidates, files_by_path, settings)
    else:
        confirmed, rejected = candidates, []

    ranked = rank(confirmed, settings.min_severity, settings.max_comments)
    inline, summary_only = place(ranked, files_by_path)
    snapped_repeats = [f for f in inline if (f.file, f.line) in seen]  # snapped onto an old comment
    inline = [f for f in inline if (f.file, f.line) not in seen]

    return ReviewResult(
        inline=inline,
        summary_only=summary_only,
        rejected=rejected,
        stats={
            "files": len(reviewable),
            "skipped": len(pr.skipped),
            "batches": len(batches),
            "agent_calls": len(tasks),
            "agent_errors": errors,
            "raw_findings": len(raw),
            "already_commented": len(repeats) + len(snapped_repeats),
            "candidates": len(candidates),
            "confirmed": len(confirmed),
            "rejected": len(rejected),
            "posted": len(inline) + len(summary_only),
        },
    )
