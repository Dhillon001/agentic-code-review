"""Sanity checks for the labelled eval cases: a broken case silently skews precision/recall."""

from __future__ import annotations

import pytest

from evals.run_evals import CASES, load_case
from reviewer.ingest.diff import parse_unified_diff

CASE_DIRS = sorted(p for p in CASES.iterdir() if p.is_dir())


def test_has_cases_including_clean_ones():
    assert len(CASE_DIRS) >= 9
    assert sum(1 for d in CASE_DIRS if not load_case(d)[0]["expected"]) >= 2


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda d: d.name)
def test_case_loads_and_expected_lines_are_commentable(case_dir):
    meta, diff, files = load_case(case_dir)
    assert meta["title"]
    diffs = {fd.path: fd for fd in parse_unified_diff(diff)}
    assert diffs, "change.diff parsed to no files"

    for e in meta["expected"]:
        assert set(e) >= {"file", "line", "category"}
        assert e["file"] in diffs, f"{e['file']} is not in change.diff"
        assert e["line"] in diffs[e["file"]].commentable_lines, f"{e['file']}:{e['line']} is not commentable"


@pytest.mark.parametrize("case_dir", CASE_DIRS, ids=lambda d: d.name)
def test_diff_matches_post_change_files(case_dir):
    _, diff, files = load_case(case_dir)
    for fd in parse_unified_diff(diff):
        if fd.status == "deleted":
            continue
        assert fd.path in files, f"files/{fd.path} missing"
        src = files[fd.path].splitlines()
        for hunk in fd.hunks:
            added = iter(hunk.added_lines)
            for raw in hunk.lines:
                if raw.startswith("+"):
                    n = next(added)
                    assert src[n - 1] == raw[1:], f"{fd.path}:{n} differs from change.diff"
