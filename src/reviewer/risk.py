"""Risk ranking for large PRs: which files the agents should see first when they can't see them all.

When a PR has more reviewable files than `max_files`, or more than `MAX_COST_USD` pays for, the files with the
lowest risk score are the ones left out (and listed in the summary). The score is a cheap, deterministic
heuristic over the diff alone — no model call:

- security-sensitive paths (`auth`, `crypto`, `sql`, `session`, `token`, `payment`, ...) rank highest;
- more changed lines (added + removed) rank higher, on a log scale so one huge file can't dwarf everything;
- new files get a small boost (no history, nothing reviewed them before);
- test files always go last.

Ties keep the original diff order, so equal-risk files behave exactly like a plain truncation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from typing import TypeVar

from reviewer.models import FileDiff

T = TypeVar("T")

# Path fragments that mark code where a missed bug is expensive. Matched case-insensitively at the start of a
# path word (so `auth/`, `oauth.py`, `user_session.go` and `mysql_store.py` hit, `author.py` does not).
SENSITIVE_PATTERNS = (
    r"auth(?!or)", r"oauth", r"login", r"passw(?:or)?d", r"secret", r"credential", r"token", r"session", r"jwt",
    r"crypt", r"cipher", r"hash", r"sign(?:ing|ature)", r"cert", r"tls", r"ssl",
    r"sql", r"\w*sql", r"quer(?:y|ies)", r"migration", r"db",
    r"permission", r"acl", r"rbac", r"polic(?:y|ies)", r"admin", r"sanitiz", r"escape", r"security",
    r"payment", r"billing", r"checkout", r"wallet",
)
_SENSITIVE_RE = re.compile(r"(?:^|[^a-z0-9])(?:" + "|".join(SENSITIVE_PATTERNS) + ")", re.IGNORECASE)

_TEST_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec|specs|testdata|fixtures)/"  # test directories
    r"|(?:^|/)test_[^/]*$|_test\.[a-z0-9]+$|(?:^|/)conftest\.py$"  # python / go
    r"|\.(?:test|spec)\.[a-z0-9]+$"  # js / ts
    r"|(?:Test|Tests|Spec)\.(?:java|kt|cs|swift|scala)$",  # jvm / .net / swift
    re.IGNORECASE,
)

SENSITIVE_WEIGHT = 10.0
NEW_FILE_WEIGHT = 2.0
TEST_PENALTY = 100.0  # bigger than any other combination: tests always go last


def is_sensitive_path(path: str) -> bool:
    return bool(_SENSITIVE_RE.search(path))


def is_test_path(path: str) -> bool:
    return bool(_TEST_RE.search(path))


def changed_line_count(diff: FileDiff) -> int:
    """Added + removed lines (context lines don't count)."""
    added = sum(len(h.added_lines) for h in diff.hunks)
    removed = sum(1 for h in diff.hunks for line in h.lines if line.startswith("-"))
    return added + removed


def risk_score(diff: FileDiff) -> float:
    """Higher = review first. See the module docstring for the ingredients."""
    score = math.log2(1 + changed_line_count(diff))
    if is_sensitive_path(diff.path):
        score += SENSITIVE_WEIGHT
    if diff.status == "added":
        score += NEW_FILE_WEIGHT
    if is_test_path(diff.path):
        score -= TEST_PENALTY
    return score


def by_risk(items: Sequence[T], diff_of: Callable[[T], FileDiff]) -> list[T]:
    """`items` riskiest first; ties keep their original order (the sort is stable)."""
    return sorted(items, key=lambda it: -risk_score(diff_of(it)))


def cap_files(diffs: Sequence[FileDiff], max_files: int) -> tuple[list[FileDiff], list[FileDiff]]:
    """`(kept, dropped)`: keep at most `max_files` reviewable diffs, dropping the lowest-risk ones.

    Deleted and hunk-less diffs never count against the cap (they cost nothing to carry). `kept` preserves the
    original diff order; `dropped` is riskiest first."""
    reviewable = [d for d in diffs if d.status != "deleted" and d.hunks]
    if len(reviewable) <= max_files:
        return list(diffs), []
    ranked = by_risk(reviewable, lambda d: d)
    dropped_ids = {id(d) for d in ranked[max(0, max_files):]}
    return [d for d in diffs if id(d) not in dropped_ids], [d for d in ranked if id(d) in dropped_ids]
