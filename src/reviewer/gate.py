"""CI gate: turn a review into a process exit code.

    0  review ran; nothing at or above the `--fail-on` severity was posted (or no gate was set)
    1  at least one posted finding is at or above the `--fail-on` severity
    2  the review itself failed (bad config, missing credentials, API error, ...)

Only *posted* findings count (inline + summary). Findings the verifier rejected never fail a build.
"""

from __future__ import annotations

from reviewer.models import Finding, ReviewResult, Severity

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

SEVERITY_CHOICES = [s.value for s in sorted(Severity, key=lambda s: s.rank)]  # low → critical


def blocking_findings(result: ReviewResult, fail_on: str | Severity | None) -> list[Finding]:
    """Posted findings at or above `fail_on`, most severe first. Empty when no gate is set."""
    if fail_on is None:
        return []
    floor = Severity(fail_on).rank
    posted = result.inline + result.summary_only
    hits = [f for f in posted if f.severity.rank >= floor]
    return sorted(hits, key=lambda f: (f.severity.rank, f.confidence), reverse=True)


def gate_exit_code(result: ReviewResult, fail_on: str | Severity | None) -> int:
    return EXIT_FINDINGS if blocking_findings(result, fail_on) else EXIT_OK


def gate_message(result: ReviewResult, fail_on: str | Severity | None) -> str | None:
    """One line for stderr explaining a failed gate, or None if the gate passed / wasn't set."""
    hits = blocking_findings(result, fail_on)
    if not hits:
        return None
    worst = hits[0]
    noun = "finding" if len(hits) == 1 else "findings"
    return (
        f"acr: {len(hits)} {noun} at or above '{Severity(fail_on).value}' "
        f"(worst: [{worst.severity.value}] {worst.file}:{worst.line} {worst.title})"
    )
