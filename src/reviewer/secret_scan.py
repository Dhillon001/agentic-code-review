"""Secret scanning pre-pass: deterministic regex detectors run on added lines only, before any model call.

A hit is a `critical` finding in the `secrets` category with `confidence=1.0`. It never goes to the verifier
(a regex match on a key format is not a judgement call) and the matched value is redacted everywhere it would
otherwise be shown, including in any agent finding that happens to quote it.

Detectors, most specific first (one finding per line, the first detector that matches wins):

    AWS access key ID            AKIA… / ASIA… (20 chars)
    GitHub token                 ghp_ / gho_ / ghu_ / ghs_ / ghr_ …, github_pat_ …
    Slack token / webhook        xoxb- / xoxp- / xoxa- / xoxr- / xoxs- …, hooks.slack.com/services/…
    Stripe key                   sk_live_ / sk_test_ / rk_live_ / rk_test_ …
    Private key                  -----BEGIN … PRIVATE KEY-----
    Generic credential           password= / api_key: / secret= / token= … with a high-entropy literal value

Silence a known-safe line with `acr: ignore[secrets]` (see reviewer/suppress.py).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from reviewer.models import FileContext, Finding, Severity

AGENT = "secrets"
CATEGORY = "secrets"

GENERIC_MIN_LEN = 12
GENERIC_MIN_ENTROPY = 3.5  # bits per character (Shannon); random 12+ char tokens sit well above this

# Values that are obviously not real credentials.
PLACEHOLDER_RE = re.compile(
    r"(?i)(x{4,}|\*{3,}|\.{3}|example|sample|dummy|placeholder|changeme|your[_-]?|redacted|<[^>]*>|\$\{|\{\{|%\()"
)

_KEY_NAME = r"[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|auth[_-]?token|access[_-]?token|token|credential)[A-Za-z0-9_.-]*"
GENERIC_RE = re.compile(
    rf"""(?ix)
    ["']?(?P<key>{_KEY_NAME})["']?            # key name, optionally quoted (JSON / dict literals)
    \s*(?::=|=>|=|:)\s*                        # assignment / mapping
    (?:
        (?P<q>["'`])(?P<quoted>[^"'`\s]+)(?P=q)  # quoted literal
      | (?P<bare>[A-Za-z0-9+/=_\-!@$%^&*~]+)\s*(?:[#;].*)?$   # unquoted to end of line (.env / yaml / ini)
    )
    """
)


@dataclass(frozen=True)
class Detector:
    name: str
    pattern: re.Pattern[str]
    group: str | int = 0  # which group holds the secret value

    def find(self, line: str) -> str | None:
        for m in self.pattern.finditer(line):
            value = m.group(self.group)
            if value:
                return value
        return None


DETECTORS: list[Detector] = [
    Detector("AWS access key ID", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    Detector("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")),
    Detector("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    Detector(
        "Slack webhook URL",
        re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"),
    ),
    Detector("Stripe API key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    Detector(
        "private key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
]


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _char_classes(s: str) -> int:
    return sum(
        (any(c.islower() for c in s), any(c.isupper() for c in s), any(c.isdigit() for c in s),
         any(not c.isalnum() for c in s))
    )


def looks_random(value: str) -> bool:
    return (
        len(value) >= GENERIC_MIN_LEN
        and shannon_entropy(value) >= GENERIC_MIN_ENTROPY
        and _char_classes(value) >= 2
        and not PLACEHOLDER_RE.search(value)
    )


def find_generic(line: str) -> str | None:
    for m in GENERIC_RE.finditer(line):
        value = m.group("quoted") or m.group("bare")
        if value and looks_random(value):
            return value
    return None


def detect(line: str) -> tuple[str, str] | None:
    """(detector name, secret value) for the first detector that fires on `line`, else None."""
    for d in DETECTORS:
        value = d.find(line)
        if value:
            return d.name, value
    value = find_generic(line)
    return ("hardcoded credential", value) if value else None


def redact(value: str) -> str:
    """Keep a short prefix (enough to recognise the key type / rotate the right one), mask the rest."""
    if value.startswith("-----BEGIN"):
        return value  # the header is not secret; the key body on the following lines is never shown
    keep = min(4, len(value) // 4)
    return value[:keep] + "*" * min(max(len(value) - keep, 4), 16)


def _finding(path: str, line: int, kind: str, value: str) -> Finding:
    shown = redact(value)
    return Finding(
        agent=AGENT,
        file=path,
        line=line,
        severity=Severity.CRITICAL,
        category=CATEGORY,
        title=f"Hardcoded {kind}",
        detail=(
            f"This line adds what looks like a {kind} (`{shown}`). Anything committed stays in git history "
            "even after it is deleted, so treat it as leaked."
        ),
        suggestion="Revoke/rotate the credential, then load it from an environment variable or a secret manager.",
        confidence=1.0,
    )


def scan_file(fc: FileContext) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    values: set[str] = set()
    for h in fc.diff.hunks:
        added = iter(h.added_lines)  # new-file numbers, in the same order as the "+" lines
        for raw in h.lines:
            if not raw.startswith("+"):
                continue
            n = next(added, None)
            if n is None:
                break
            hit = detect(raw[1:])
            if hit:
                kind, value = hit
                findings.append(_finding(fc.path, n, kind, value))
                values.add(value)
    return findings, values


def scan(files: list[FileContext]) -> tuple[list[Finding], set[str]]:
    """Findings for every added line that carries a secret, plus the raw values (for `scrub`)."""
    findings: list[Finding] = []
    values: set[str] = set()
    for fc in files:
        if fc.diff.status == "deleted" or fc.diff.binary:
            continue
        f, v = scan_file(fc)
        findings.extend(f)
        values |= v
    return findings, values


def scrub(findings: list[Finding], values: set[str]) -> list[Finding]:
    """Redact detected secret values from any finding's text (an agent may quote the offending line).
    A `replacement` that still contains the secret is dropped rather than half-redacted."""
    values = {v for v in values if not v.startswith("-----BEGIN")}
    if not values:
        return findings

    def clean(text: str | None) -> str | None:
        if text is None:
            return None
        for v in values:
            text = text.replace(v, redact(v))
        return text

    out = []
    for f in findings:
        fields = (f.title, f.detail, f.suggestion, f.verifier_note, f.replacement)
        if not any(t and any(v in t for v in values) for t in fields):
            out.append(f)
            continue
        replacement = f.replacement if not (f.replacement and any(v in f.replacement for v in values)) else None
        out.append(
            f.model_copy(
                update={
                    "title": clean(f.title),
                    "detail": clean(f.detail),
                    "suggestion": clean(f.suggestion),
                    "verifier_note": clean(f.verifier_note),
                    "replacement": replacement,
                }
            )
        )
    return out
