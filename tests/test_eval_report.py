"""Per-category eval scores, the Markdown report, and baseline regression checks — pure functions only."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from evals import run_evals
from evals.run_evals import build_report, category_breakdown, compare_baseline, render_markdown, score
from reviewer.models import Finding


def _finding(file: str, line: int, category: str) -> Finding:
    return Finding(agent="a", file=file, line=line, severity="high", category=category,
                   title="t", detail="b", confidence=0.9)


def _rows() -> list[dict]:
    # Case 1: a security bug found (under "security"), plus a stray performance nitpick.
    s1 = score([{"file": "a.py", "line": 10, "category": "security"}],
               [_finding("a.py", 11, "security"), _finding("a.py", 40, "performance")])
    # Case 2: a correctness bug found but mislabelled as "style"; a second correctness bug missed.
    s2 = score([{"file": "b.py", "line": 5, "category": "correctness"},
                {"file": "b.py", "line": 30, "category": "correctness"}],
               [_finding("b.py", 6, "style")])
    # Case 3: clean, and the reviewer stayed quiet.
    s3 = score([], [])
    return [{"case": "one", **s1, "findings": ["a.py:11 [high/security] t | pipe"]},
            {"case": "two", **s2, "findings": []},
            {"case": "clean", **s3, "findings": []}]


def test_score_keeps_per_item_outcomes():
    s = _rows()[0]
    assert (s["tp"], s["fp"], s["fn"]) == (1, 1, 0)
    assert s["expected_hits"] == [{"category": "security", "hit": True}]
    assert s["found_hits"] == [{"category": "security", "hit": True}, {"category": "performance", "hit": False}]


def test_category_breakdown():
    cats = category_breakdown(_rows())
    assert set(cats) == {"security", "performance", "correctness", "style"}

    assert cats["security"] == {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert cats["performance"]["precision"] == 0.0 and cats["performance"]["recall"] == 1.0
    assert cats["performance"]["f1"] == 0.0
    # Two correctness bugs, one caught (under another label): recall halves, no findings of its own.
    assert cats["correctness"]["recall"] == 0.5 and cats["correctness"]["precision"] == 1.0
    assert (cats["correctness"]["tp"], cats["correctness"]["fn"]) == (1, 1)
    # The mislabelled hit is credited to the reviewer's category for precision.
    assert cats["style"]["precision"] == 1.0 and cats["style"]["fp"] == 0


def test_build_report_totals():
    report = build_report(_rows(), verify=True)
    assert (report["tp"], report["fp"], report["fn"]) == (2, 1, 1)
    assert report["precision"] == pytest.approx(2 / 3)
    assert report["recall"] == pytest.approx(2 / 3)
    assert report["f1"] == pytest.approx(2 / 3)
    assert report["categories"]["security"]["f1"] == 1.0
    json.dumps(report)  # report.json must stay serialisable


def test_empty_report_is_vacuously_perfect():
    report = build_report([], verify=False)
    assert report["precision"] == report["recall"] == 1.0 and report["categories"] == {}


def test_render_markdown():
    md = render_markdown(build_report(_rows(), verify=False))
    assert md.startswith("# Eval report\n")
    assert "verifier off" in md
    assert "| `security` | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 |" in md
    assert "| `correctness` | 1 | 0 | 1 | 1.00 | 0.50 | 0.67 |" in md
    assert "| ❌ | `one` |" in md and "| ✅ | `clean` |" in md
    assert "t \\| pipe" in md  # pipes in findings don't break the table


def test_compare_baseline_within_tolerance():
    report = build_report(_rows(), verify=True)
    baseline = {**report, "f1": report["f1"] + 0.01}
    lines, regressed = compare_baseline(report, baseline, tolerance=0.02)
    assert not regressed
    assert any(line.startswith("f1") and "(-0.01)" in line for line in lines)
    assert lines[-1].startswith("F1 within tolerance")


def test_compare_baseline_flags_regression_and_category_changes():
    report = build_report(_rows(), verify=True)
    baseline = {"precision": 1.0, "recall": 1.0, "f1": 1.0,
                "categories": {"security": {"f1": 0.5}, "secrets": {"f1": 1.0}}}
    lines, regressed = compare_baseline(report, baseline, tolerance=0.1)
    assert regressed
    text = "\n".join(lines)
    assert "security f1" in text and "(+0.50)" in text
    assert "secrets f1" in text and "gone" in text
    assert "performance f1" in text and "new" in text
    assert "REGRESSION" in lines[-1]


def test_compare_baseline_drop_exactly_at_tolerance_passes():
    report = {"precision": 0.8, "recall": 0.8, "f1": 0.8, "categories": {}}
    _, regressed = compare_baseline(report, {**report, "f1": 0.85}, tolerance=0.05)
    assert not regressed


@pytest.mark.parametrize("baseline_f1,expected_code", [(0.0, 0), (1.0, 1)])
def test_main_writes_markdown_and_gates_on_baseline(tmp_path, monkeypatch, baseline_f1, expected_code):
    row = {**score([{"file": "a.py", "line": 1, "category": "security"}], []), "case": "x", "findings": [],
           "rejected_by_verifier": 0, "calls": 0, "response_cache_hits": 0, "est_usd": 0.0}

    async def fake_run_case(d, llm, settings):
        return {**row, "case": d.name}

    eval_dir = tmp_path / "evals"
    (eval_dir / "cases" / "only-case").mkdir(parents=True)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"precision": 1.0, "recall": 1.0, "f1": baseline_f1, "categories": {}}))
    md = tmp_path / "eval.md"

    monkeypatch.setattr(run_evals, "run_case", fake_run_case)
    monkeypatch.setattr(run_evals, "make_llm", lambda settings: object())
    monkeypatch.setattr(run_evals, "CASES", eval_dir / "cases")
    monkeypatch.setattr(run_evals, "__file__", str(eval_dir / "run_evals.py"))
    monkeypatch.setattr(sys, "argv", ["run_evals.py", "--report-md", str(md),
                                      "--baseline", str(baseline), "--tolerance", "0.1"])

    assert asyncio.run(run_evals.main()) == expected_code
    assert md.read_text().startswith("# Eval report")
    written = json.loads((eval_dir / "report.json").read_text())
    assert written["categories"]["security"]["recall"] == 0.0
