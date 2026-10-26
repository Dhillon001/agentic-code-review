"""Second pass: a stronger model re-checks every candidate against the code. Unverified = dropped."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from reviewer.agents.llm import LLM
from reviewer.agents.prompts import VERDICTS_TOOL, VERIFIER_SYSTEM, with_team_rules
from reviewer.config import Settings
from reviewer.context.retriever import render_file_excerpt
from reviewer.models import FileContext, Finding, Severity


async def _verify_file(
    llm: LLM, fc: FileContext, findings: list[Finding], settings: Settings
) -> tuple[list[Finding], list[Finding]]:
    listing = [
        {"index": i, **f.model_dump(include={"line", "severity", "category", "title", "detail"}, mode="json")}
        for i, f in enumerate(findings)
    ]
    user = (
        f"=== FILE: {fc.path} ===\n--- diff ---\n{fc.diff.render_numbered()}\n"
        f"--- file after change ---\n{render_file_excerpt(fc, settings, {f.line for f in findings})}\n\n"
        f"=== FINDINGS TO VERIFY ===\n{json.dumps(listing, indent=2)}"
    )
    data = await llm.call_tool(
        model=settings.verifier_model,
        system=with_team_rules(VERIFIER_SYSTEM, settings.rules),
        user=user,
        tool=VERDICTS_TOOL,
    )
    verdicts = {v.get("index"): v for v in data.get("verdicts", [])}

    confirmed, rejected = [], []
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        if v and v.get("verdict") == "confirmed":
            update = {"verified": True, "verifier_note": v.get("note")}
            if v.get("severity") in Severity._value2member_map_:
                update["severity"] = Severity(v["severity"])
            confirmed.append(f.model_copy(update=update))
        else:  # missing verdict is treated as rejection — conservative by design
            rejected.append(f.model_copy(update={"verifier_note": v.get("note") if v else "no verdict"}))
    return confirmed, rejected


async def verify(
    llm: LLM, findings: list[Finding], files_by_path: dict[str, FileContext], settings: Settings
) -> tuple[list[Finding], list[Finding]]:
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_file[f.file].append(f)

    results = await asyncio.gather(
        *(_verify_file(llm, files_by_path[p], fs, settings) for p, fs in by_file.items() if p in files_by_path)
    )
    confirmed = [f for c, _ in results for f in c]
    rejected = [f for _, r in results for f in r]
    return confirmed, rejected
