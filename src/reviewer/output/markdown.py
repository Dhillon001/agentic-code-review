"""Standalone Markdown report (`acr ... --format markdown`).

Unlike the GitHub review (inline comments + summary), this is one self-contained document: findings grouped
by file, most severe file first, each with its severity icon, detail, a ```suggestion block when the finding
carries a single-line replacement, and the funnel stats + usage footer at the end. Good for CI job summaries
(`$GITHUB_STEP_SUMMARY`), PR-less reviews, and archiving.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from reviewer.models import Finding, ReviewResult
from reviewer.output.github_review import (
    ICON,
    budget_note,
    partial_note,
    suggestion_block,
)
from reviewer.usage import format_usage


def _code(text: str) -> str:
    """Inline code span that survives backticks in the text (e.g. odd file names)."""
    runs = [len(m) for m in re.findall(r"`+", text)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _finding(f: Finding, off_diff: bool) -> list[str]:
    sev = f.severity.value
    where = f"line {f.line}" + (" · outside the diff" if off_diff else "")
    out = [f"### {ICON[sev]} {sev.title()} · {f.category} — {f.title}", "", f"<sub>{where}</sub>", "", f.detail]
    replacement = f.single_line_replacement
    if replacement is not None:
        out += ["", "**Suggested change:**" + (f" {f.suggestion}" if f.suggestion else ""), "",
                suggestion_block(replacement)]
    elif f.suggestion:
        out += ["", f"**Suggested fix:** {f.suggestion}"]
    meta = f"{f.agent or 'agent'} · confidence {f.confidence:.0%}" + (" · verified" if f.verified else "")
    out += ["", f"<sub>{meta}</sub>", ""]
    return out


def format_markdown(result: ReviewResult, title: str = "Agentic review") -> str:
    posted = [(f, False) for f in result.inline] + [(f, True) for f in result.summary_only]
    lines = [f"# 🤖 {title}", ""]
    if result.partial:
        lines += [f"> {partial_note(result)}", ""]
    if result.skipped_budget:
        lines += [f"> {budget_note(result)}", ""]

    if not posted:
        lines += ["✅ No issues survived verification.", ""]
    else:
        counts = Counter(f.severity.value for f, _ in posted)
        noun = "finding" if len(posted) == 1 else "findings"
        tally = " · ".join(f"{ICON[s]} {counts[s]} {s}" for s in ICON if counts[s])
        lines += [f"**{len(posted)} {noun}:** {tally}", ""]

        by_file: dict[str, list[tuple[Finding, bool]]] = defaultdict(list)
        for f, off in posted:
            by_file[f.file].append((f, off))
        # Most severe file first, then by path; within a file, by line (most severe first on ties).
        order = sorted(by_file, key=lambda p: (-max(f.severity.rank for f, _ in by_file[p]), p))
        for path in order:
            items = sorted(by_file[path], key=lambda x: (x[0].line, -x[0].severity.rank))
            lines += [f"## {_code(path)}", ""]
            for f, off in items:
                lines += _finding(f, off)

    lines.append("---")
    lines.append("")
    s = result.stats
    funnel = (
        f"{s.get('raw_findings', 0)} raw → {s.get('candidates', 0)} candidates → "
        f"{s.get('confirmed', 0)} verified · {s.get('rejected', 0)} rejected by verifier"
    )
    if s.get("suppressed"):
        funnel += f" · {s['suppressed']} suppressed"
    lines.append(f"<sub>{funnel}</sub>")
    if result.usage.calls:
        lines += ["", f"<sub>💸 {format_usage(result.usage)}</sub>"]
    return "\n".join(lines) + "\n"
