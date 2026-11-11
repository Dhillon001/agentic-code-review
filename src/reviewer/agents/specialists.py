from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from reviewer.agents.llm import LLM, UserContent, cached_text, text_block
from reviewer.agents.prompts import COMMON_RULES, FINDINGS_TOOL, SPECIALIST_FOCUS, with_team_rules
from reviewer.config import Settings
from reviewer.context.retriever import render_file_excerpt
from reviewer.models import FileContext, Finding, PRContext, Severity
from reviewer.repo_config import rules_for_paths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Specialist:
    name: str
    focus: str
    severity_floor: Severity | None = None  # this agent's findings below it are dropped

    def keeps(self, f: Finding) -> bool:
        return self.severity_floor is None or f.severity.rank >= self.severity_floor.rank

    @property
    def instructions(self) -> str:
        """The per-specialist part of the prompt. Sent *after* the shared PR context so that the
        system prompt and context prefix are byte-identical across specialists (prompt caching)."""
        return (
            f"You are the `{self.name}` reviewer.\nFocus ONLY on: {self.focus}\n"
            "Review the pull request above from that angle only and report your findings."
        )


def reviewer_system(rules: str = "") -> str:
    """System prompt shared by every specialist. Must not vary per specialist, or the cache misses.
    (It may vary per batch: `paths:` rules are only included for batches containing matching files.)"""
    return with_team_rules(COMMON_RULES, rules)


SPECIALISTS = [Specialist(name, focus) for name, focus in SPECIALIST_FOCUS.items()]
BUILTIN_NAMES = frozenset(SPECIALIST_FOCUS)
# names already used for findings that don't come from a specialist (secret pre-pass, verifier)
RESERVED_NAMES = frozenset({"secrets", "verifier"})


def build_specialists(settings: Settings) -> list[Specialist]:
    """Built-ins plus `custom_agents`, minus `disable_agents`. Never raises: a custom agent whose name
    clashes with a built-in or reserved name is skipped (later duplicates of a custom name win), and
    disabling a name that doesn't exist only logs a warning."""
    custom: dict[str, Specialist] = {}
    for agent in settings.custom_agents:
        if agent.name in BUILTIN_NAMES or agent.name in RESERVED_NAMES:
            log.warning(
                "skipping custom agent %r: name is taken by a built-in (disable it and pick another name)",
                agent.name,
            )
            continue
        custom[agent.name] = Specialist(agent.name, agent.focus, agent.severity_floor)

    pool = [*SPECIALISTS, *custom.values()]
    known = {s.name for s in pool}
    disabled = {str(n).strip().lower() for n in settings.disable_agents if str(n).strip()}
    for name in sorted(disabled - known):
        log.warning("ignoring unknown agent in disable list: %r (known: %s)", name, ", ".join(sorted(known)))
    for name in sorted(set(settings.agent_models) - known):
        log.warning("ignoring model route for unknown agent %r (known: %s)", name, ", ".join(sorted(known)))
    return [s for s in pool if s.name not in disabled]


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


def build_specialist_content(spec: Specialist, shared_context: str) -> UserContent:
    """Shared PR context first (cache breakpoint), specialist focus last."""
    return [cached_text(shared_context), text_block(spec.instructions)]


async def run_specialist(
    llm: LLM,
    spec: Specialist,
    pr: PRContext,
    files: list[FileContext],
    settings: Settings,
    shared_context: str | None = None,
) -> list[Finding]:
    if shared_context is None:
        shared_context = build_review_prompt(pr, files, settings)
    data = await llm.call_tool(
        model=settings.model_for(spec.name),
        system=reviewer_system(rules_for_paths(settings.rules, settings.path_rules, [fc.path for fc in files])),
        user=build_specialist_content(spec, shared_context),
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
        if not spec.keeps(f):
            continue
        out.append(f)
    return out
