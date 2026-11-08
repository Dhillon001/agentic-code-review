"""Inline suppressions: let authors silence a finding where it lives, in any comment syntax.

    risky_call()  # acr: ignore                      → no findings on this line
    risky_call()  // acr: ignore[security]           → only findings from the security agent / category
    # acr: ignore-next-line[performance, n-plus-one]
    for u in users: db.load(u)                       → next line, only those agents/categories

A scope token matches a finding's agent name (`security`) or its category slug (`sql-injection`),
case-insensitively. Directives are read from the post-change file, so a PR can add its own.
"""

from __future__ import annotations

import re

from reviewer.models import FileContext, Finding

DIRECTIVE_RE = re.compile(r"\bacr:\s*ignore(?P<next>-next-line)?(?:\[(?P<scope>[^\]]*)\])?", re.IGNORECASE)

# line number -> None (suppress everything) or the set of lowercase agent/category tokens to suppress
LineRules = dict[int, "frozenset[str] | None"]


def parse_suppressions(content: str | None) -> LineRules:
    rules: dict[int, frozenset[str] | None] = {}
    if not content:
        return rules
    for i, text in enumerate(content.splitlines(), start=1):
        for m in DIRECTIVE_RE.finditer(text):
            target = i + 1 if m.group("next") else i
            scope = m.group("scope")
            tokens = frozenset(t.strip().lower() for t in scope.split(",") if t.strip()) if scope else None
            if not tokens:  # bare `acr: ignore` or an empty `[]` means everything
                rules[target] = None
            elif target not in rules or rules[target] is not None:
                rules[target] = (rules.get(target) or frozenset()) | tokens
    return rules


def is_suppressed(f: Finding, rules: LineRules) -> bool:
    if f.line not in rules:
        return False
    scope = rules[f.line]
    return scope is None or f.agent.lower() in scope or f.category.lower() in scope


def suppressions_for(files: list[FileContext]) -> dict[str, LineRules]:
    return {fc.path: parse_suppressions(fc.content) for fc in files}


def split_suppressed(
    findings: list[Finding], by_file: dict[str, LineRules]
) -> tuple[list[Finding], list[Finding]]:
    """(kept, suppressed)."""
    kept, dropped = [], []
    for f in findings:
        (dropped if is_suppressed(f, by_file.get(f.file, {})) else kept).append(f)
    return kept, dropped
