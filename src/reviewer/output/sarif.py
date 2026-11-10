"""SARIF 2.1.0 report (`acr ... --format sarif`) for GitHub code scanning.

Upload it with `github/codeql-action/upload-sarif` and findings show up as code-scanning alerts on the PR and
in the repo's Security tab. Mapping:

* one **rule** per finding category (`sql-injection`, `bug`, …), with the most severe level seen as its default;
* **level** from severity: critical/high → `error`, medium → `warning`, low → `note`;
* a **physical location** per finding (repo-relative URI under `%SRCROOT%`, start line);
* **partial fingerprints** hashed from (file, category, title) — deliberately not the line, so an alert keeps its
  identity when code above it moves and GitHub doesn't reopen it as new;
* a single-line `replacement` becomes a SARIF **fix** that replaces the whole line.

Verifier-rejected findings are never emitted; off-diff findings are, flagged with `outsideDiff: true`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

from reviewer import __version__
from reviewer.models import Finding, ReviewResult, Severity

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
TOOL_NAME = "agentic-code-review"
TOOL_URI = "https://github.com/Dhillon001/agentic-code-review"
FINGERPRINT_KEY = "acrFindingHash/v1"

LEVEL: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
}
# GitHub code scanning reads `security-severity` (0.0–10.0, CVSS-like) on rules tagged `security`.
SECURITY_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "9.5",
    Severity.HIGH: "8.0",
    Severity.MEDIUM: "5.5",
    Severity.LOW: "2.0",
}


def rule_id(category: str) -> str:
    """A stable, URI-safe rule id from a free-text category slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", category.strip().lower()).strip("-")
    return slug or "uncategorized"


def _rule_name(rid: str) -> str:
    return "".join(part.capitalize() for part in rid.split("-")) or "Uncategorized"


def _uri(path: str) -> str:
    """Repo-relative POSIX path as a URI reference (spaces, `#`, `%` etc. percent-encoded)."""
    posix = path.replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    return quote(posix.lstrip("/"), safe="/-._~")


def fingerprint(f: Finding) -> str:
    title = " ".join(f.title.lower().split())
    key = "\x1f".join([f.file, rule_id(f.category), title])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _is_security(findings: list[Finding]) -> bool:
    return any(f.agent == "security" or "security" in f.category.lower() for f in findings)


def _message(f: Finding) -> dict[str, str]:
    text = f"{f.title}\n\n{f.detail}"
    if f.suggestion:
        text += f"\n\nSuggested fix: {f.suggestion}"
    return {"text": text}


def _result(f: Finding, rule_index: int, off_diff: bool) -> dict[str, Any]:
    line = max(f.line, 1)
    out: dict[str, Any] = {
        "ruleId": rule_id(f.category),
        "ruleIndex": rule_index,
        "level": LEVEL[f.severity],
        "message": _message(f),
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": _uri(f.file), "uriBaseId": "%SRCROOT%"},
                    "region": {"startLine": line},
                }
            }
        ],
        "partialFingerprints": {FINGERPRINT_KEY: fingerprint(f)},
        "properties": {
            "severity": f.severity.value,
            "agent": f.agent,
            "confidence": round(f.confidence, 3),
            "verified": f.verified,
            "outsideDiff": off_diff,
        },
    }
    replacement = f.single_line_replacement
    if replacement is not None:
        out["fixes"] = [
            {
                "description": {"text": f.suggestion or f"Replace line {line}"},
                "artifactChanges": [
                    {
                        "artifactLocation": {"uri": _uri(f.file), "uriBaseId": "%SRCROOT%"},
                        "replacements": [
                            {
                                # The whole line, from column 1 to the start of the next line.
                                "deletedRegion": {"startLine": line, "startColumn": 1,
                                                  "endLine": line + 1, "endColumn": 1},
                                "insertedContent": {"text": replacement + "\n"},
                            }
                        ],
                    }
                ],
            }
        ]
    return out


def build_sarif(result: ReviewResult) -> dict[str, Any]:
    posted = [(f, False) for f in result.inline] + [(f, True) for f in result.summary_only]
    posted.sort(key=lambda x: (x[0].file, x[0].line, -x[0].severity.rank, x[0].title))

    by_rule: dict[str, list[Finding]] = {}
    for f, _ in posted:
        by_rule.setdefault(rule_id(f.category), []).append(f)

    rules: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for rid in sorted(by_rule):
        findings = by_rule[rid]
        worst = max((f.severity for f in findings), key=lambda s: s.rank)
        security = _is_security(findings)
        props: dict[str, Any] = {"tags": ["security"] if security else ["quality"]}
        if security:
            props["security-severity"] = SECURITY_SEVERITY[worst]
        index[rid] = len(rules)
        rules.append(
            {
                "id": rid,
                "name": _rule_name(rid),
                "shortDescription": {"text": rid.replace("-", " ").capitalize()},
                "defaultConfiguration": {"level": LEVEL[worst]},
                "properties": props,
            }
        )

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": TOOL_NAME,
                "version": __version__,
                "semanticVersion": __version__,
                "informationUri": TOOL_URI,
                "rules": rules,
            }
        },
        "originalUriBaseIds": {"%SRCROOT%": {"description": {"text": "Repository root"}}},
        "columnKind": "utf16CodeUnits",
        "results": [_result(f, index[rule_id(f.category)], off) for f, off in posted],
        "properties": {"stats": dict(result.stats)},
    }
    if result.usage.calls:
        run["properties"]["usage"] = result.usage.model_dump(mode="json")
    return {"$schema": SARIF_SCHEMA, "version": SARIF_VERSION, "runs": [run]}


def format_sarif(result: ReviewResult) -> str:
    return json.dumps(build_sarif(result), indent=2, ensure_ascii=False) + "\n"
