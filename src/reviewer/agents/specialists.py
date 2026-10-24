from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from reviewer.agents.llm import LLM
from reviewer.agents.prompts import COMMON_RULES, FINDINGS_TOOL, SPECIALIST_FOCUS
from reviewer.config import Settings
from reviewer.context.retriever import render_file_excerpt
from reviewer.models import FileContext, Finding, PRContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Specialist:
    name: str
    focus: str

    @property
    def system(self) -> str:
        return f"You are the `{self.name}` reviewer.\nFocus ONLY on: {self.focus}\n\n{COMMON_RULES}"


SPECIALISTS = [Specialist(name, focus) for name, focus in SPECIALIST_FOCUS.items()]


def render_files(files: list[FileContext], settings: Settings) -> str:
    parts = []
    for fc in files:
        parts.append(f"=== FILE: {fc.path} ({fc.diff.status}) ===")
        parts.append("--- diff (new-file line numbers) ---")
        parts.append(fc.diff.render_numbered())
        parts.append("--- file after change ---")
        parts.append(render_file_excerpt(fc, settings))
        for r in fc.related:
            parts.append(f"--- related: {r.path} ({r.reason}) ---")
            parts.append(r.text)
        parts.append("")
    return "\n".join(parts)


def build_review_prompt(pr: PRContext, files: list[FileContext], settings: Settings) -> str:
    header = f"PR: {pr.title or '(untitled)'}\n"
    if pr.body:
        header += f"Description:\n{pr.body[:3000]}\n"
    return f"{header}\n{render_files(files, settings)}"


async def run_specialist(
    llm: LLM, spec: Specialist, pr: PRContext, files: list[FileContext], settings: Settings
) -> list[Finding]:
    data = await llm.call_tool(
        model=settings.reviewer_model,
        system=spec.system,
        user=build_review_prompt(pr, files, settings),
        tool=FINDINGS_TOOL,
    )
    allowed = {fc.path for fc in files}
    out: list[Finding] = []
    for raw in data.get("findings", []):
        try:
            f = Finding.model_validate({**raw, "agent": spec.name})
        except ValidationError as e:
            log.warning("dropping malformed finding from %s: %s", spec.name, e)
            continue
        if f.file not in allowed:
            log.warning("dropping finding for file not in batch: %s", f.file)
            continue
        out.append(f)
    return out
