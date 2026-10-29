"""Dedupe, rank, cap, and anchor findings to lines GitHub will accept."""

from __future__ import annotations

from reviewer.models import FileContext, Finding, Severity


def dedupe(findings: list[Finding], window: int = 2) -> list[Finding]:
    """Collapse near-duplicates (same file, nearby line, same category or same line) across agents."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: (x.severity.rank, x.confidence), reverse=True):
        dup = any(
            k.file == f.file
            and abs(k.line - f.line) <= window
            and (k.category == f.category or k.line == f.line)
            for k in kept
        )
        if not dup:
            kept.append(f)
    return kept


def rank(findings: list[Finding], min_severity: str, max_comments: int) -> list[Finding]:
    floor = Severity(min_severity).rank
    eligible = [f for f in findings if f.severity.rank >= floor]
    eligible.sort(key=lambda x: (x.severity.rank, x.confidence), reverse=True)
    return eligible[:max_comments]


def place(
    findings: list[Finding], files_by_path: dict[str, FileContext], snap: int = 3
) -> tuple[list[Finding], list[Finding]]:
    """Split into inline (line is in the diff) vs summary-only. Snap to a nearby diff line if close
    (dropping any `replacement`, which only applies to the line it was written for)."""
    inline, summary = [], []
    for f in findings:
        fc = files_by_path.get(f.file)
        lines = fc.diff.commentable_lines if fc else set()
        if f.line in lines:
            inline.append(f)
            continue
        near = sorted((ln for ln in lines if abs(ln - f.line) <= snap), key=lambda ln: abs(ln - f.line))
        if near:
            # a replacement was written for the original line; applied to the snapped one it would clobber it
            inline.append(f.model_copy(update={"line": near[0], "replacement": None}))
        else:
            summary.append(f)
    return inline, summary
