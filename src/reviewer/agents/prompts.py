"""Prompts and tool schemas. Tune here; the pipeline doesn't care about wording."""

SEVERITIES = ["critical", "high", "medium", "low"]

COMMON_RULES = """\
You are reviewing a pull request. Rules:
- Only report issues INTRODUCED or EXPOSED by the changed lines. The diff shows new-file line
  numbers in the left gutter; cite the exact line number of the problem.
- Report real defects only. No style nits, naming bikesheds, or "consider adding a comment".
- Every finding must say concretely what goes wrong (input/state -> bad outcome).
- confidence is your honest probability the issue is real (0-1). Prefer fewer, sharper findings.
- An empty findings list is a perfectly good answer.
"""

TEAM_RULES_HEADER = """\
Team conventions for this repository (from its .acr.yml). A clear violation on a changed line is a
real finding (category `team-rule`); do not stretch them to cover code they don't address:
"""


def with_team_rules(system: str, rules: str) -> str:
    return f"{system}\n{TEAM_RULES_HEADER}{rules}\n" if rules.strip() else system


SPECIALIST_FOCUS = {
    "correctness": (
        "Logic and correctness: off-by-one errors, wrong conditions, null/None handling, broken "
        "invariants, incorrect error handling, race conditions, API contract changes that break callers."
    ),
    "security": (
        "Security: injection (SQL, shell, template), authn/authz gaps, secrets in code, unsafe "
        "deserialization, path traversal, SSRF, missing input validation at trust boundaries."
    ),
    "performance": (
        "Performance: N+1 queries, accidental quadratic loops, unbounded memory/IO, blocking calls "
        "in async code, missing pagination, work repeated inside hot loops. Only flag issues that "
        "matter at realistic scale."
    ),
    "tests": (
        "Test coverage: new behaviour or a bug-prone branch with no test, tests that assert nothing "
        "meaningful, tests changed to pass instead of fixing code. Point at the untested changed line."
    ),
}

FINDINGS_TOOL = {
    "name": "report_findings",
    "description": "Report code review findings for the changed lines.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer", "description": "new-file line number"},
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "category": {"type": "string", "description": "short kebab-case slug"},
                        "title": {"type": "string", "description": "<= 80 chars"},
                        "detail": {"type": "string", "description": "what breaks, concretely"},
                        "suggestion": {"type": "string", "description": "how to fix (optional)"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["file", "line", "severity", "category", "title", "detail", "confidence"],
                },
            }
        },
        "required": ["findings"],
    },
}

VERIFIER_SYSTEM = """\
You are a skeptical senior engineer verifying another reviewer's findings before they are posted
on a pull request. False positives waste the author's time and erode trust in the tool.

For each finding, check it against the actual code shown:
- confirmed: the issue is real, on (or right next to) the cited line, and caused/exposed by this change.
- rejected: speculative, already handled elsewhere in the shown code, a style preference, wrong line,
  or not something this PR introduced.
You may adjust severity if the original is clearly over- or under-stated. Be brief in notes.
"""

VERDICTS_TOOL = {
    "name": "report_verdicts",
    "description": "Return a verdict for every finding, by index.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["confirmed", "rejected"]},
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "note": {"type": "string"},
                    },
                    "required": ["index", "verdict", "note"],
                },
            }
        },
        "required": ["verdicts"],
    },
}
