"""`run_evals.py --repeat N`: mean ± std across runs and flaky-case detection — pure functions plus main()."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from evals import run_evals
from evals.run_evals import build_repeated_report, case_outcomes, mean_std, render_markdown, score
from reviewer.models import Finding


def _finding(file: str, line: int) -> Finding:
    return Finding(agent="a", file=file, line=line, severity="high", category="security",
                   title="t", detail="b", confidence=0.9)


BUG = [{"file": "a.py", "line": 10, "category": "security"}]


def _row(case: str, expected: list[dict], found: list[Finding]) -> dict:
    return {"case": case, **score(expected, found), "findings": []}


def _runs() -> list[list[dict]]:
    # "bug" is caught in run 1, missed in run 2 (flaky); "clean" stays quiet both times (stable).
    return [
        [_row("bug", BUG, [_finding("a.py", 10)]), _row("clean", [], [])],
        [_row("bug", BUG, []), _row("clean", [], [])],
    ]


def test_mean_std():
    assert mean_std([1.0]) == {"mean": 1.0, "std": 0.0}
    ms = mean_std([1.0, 0.0])
    assert ms["mean"] == 0.5 and ms["std"] == pytest.approx(0.7071, abs=1e-4)  # sample std
    assert mean_std([]) == {"mean": 0.0, "std": 0.0}


def test_case_outcomes_flags_only_changed_cases():
    out = {c["case"]: c for c in case_outcomes(_runs())}
    assert out["bug"] == {"case": "bug", "runs": 2, "passes": 1, "outcomes": [[1, 0, 0], [0, 0, 1]], "flaky": True}
    assert out["clean"]["flaky"] is False and out["clean"]["passes"] == 2


def test_failing_but_consistent_case_is_not_flaky():
    runs = [[_row("miss", BUG, [])], [_row("miss", BUG, [])]]
    (c,) = case_outcomes(runs)
    assert c["passes"] == 0 and not c["flaky"]


def test_build_repeated_report():
    report = build_repeated_report(_runs(), verify=True)
    var = report["variance"]
    assert var["runs"] == 2 and var["flaky_cases"] == ["bug"]
    # Run 1: P=R=F1=1. Run 2: nothing found, one missed → P=1 (vacuous), R=0, F1=0.
    assert [r["recall"] for r in var["per_run"]] == [1.0, 0.0]
    assert var["precision"] == {"mean": 1.0, "std": 0.0}
    assert var["f1"]["mean"] == 0.5 and var["f1"]["std"] == pytest.approx(0.7071, abs=1e-4)
    # Headline metrics are the means (what --baseline compares); counts and categories pool both runs.
    assert report["f1"] == 0.5 and report["recall"] == 0.5
    assert (report["tp"], report["fp"], report["fn"]) == (1, 0, 1)
    assert report["categories"]["security"]["recall"] == 0.5
    assert [(r["case"], r["run"]) for r in report["cases"]] == [("bug", 1), ("clean", 1), ("bug", 2), ("clean", 2)]
    json.dumps(report)


def test_identical_runs_have_zero_variance():
    runs = [_runs()[0], _runs()[0]]
    var = build_repeated_report(runs, verify=False)["variance"]
    assert all(var[m]["std"] == 0.0 for m in ("precision", "recall", "f1"))
    assert var["flaky_cases"] == []


def test_render_markdown_variance_section():
    md = render_markdown(build_repeated_report(_runs(), verify=True))
    assert "## Variance (2 runs)" in md
    assert "| f1 | 0.50 | 0.71 |" in md
    assert "**Flaky cases:** `bug`" in md
    assert "| ⚠️ `bug` | 1/2 | 1/0/0, 0/0/1 |" in md
    assert "| `clean` | 2/2 | 0/0/0, 0/0/0 |" in md
    assert "`bug` (run 2)" in md


def test_single_run_markdown_has_no_variance_section():
    md = render_markdown(run_evals.build_report(_runs()[0], verify=True))
    assert "Variance" not in md and "(run " not in md


def _patch_main(monkeypatch, tmp_path, argv, fake_run_case):
    eval_dir = tmp_path / "evals"
    (eval_dir / "cases" / "bug").mkdir(parents=True)
    (eval_dir / "cases" / "clean").mkdir()
    monkeypatch.setattr(run_evals, "run_case", fake_run_case)
    monkeypatch.setattr(run_evals, "make_llm", lambda settings: object())
    monkeypatch.setattr(run_evals, "CASES", eval_dir / "cases")
    monkeypatch.setattr(run_evals, "__file__", str(eval_dir / "run_evals.py"))
    monkeypatch.setattr(sys, "argv", ["run_evals.py", *argv])
    return eval_dir


def test_main_repeat_runs_each_case_n_times(tmp_path, monkeypatch, capsys):
    calls: dict[str, int] = {}

    async def fake_run_case(d, llm, settings):
        n = calls[d.name] = calls.get(d.name, 0) + 1
        extra = {"rejected_by_verifier": 0, "calls": 1, "response_cache_hits": 0, "est_usd": 0.0}
        if d.name == "clean":
            return {**_row("clean", [], []), **extra}
        # The bug is caught on odd runs only.
        return {**_row("bug", BUG, [_finding("a.py", 10)] if n % 2 else []), **extra}

    eval_dir = _patch_main(monkeypatch, tmp_path, ["--repeat", "3", "--report-md", str(tmp_path / "e.md")],
                           fake_run_case)
    assert asyncio.run(run_evals.main()) == 0
    assert calls == {"bug": 3, "clean": 3}

    written = json.loads((eval_dir / "report.json").read_text())
    assert written["variance"]["runs"] == 3 and written["variance"]["flaky_cases"] == ["bug"]
    assert written["recall"] == pytest.approx(2 / 3)
    out = capsys.readouterr().out
    assert "passed 2/3" in out and "FLAKY" in out and "flaky cases: bug" in out
    assert "## Variance (3 runs)" in (tmp_path / "e.md").read_text()


def test_main_rejects_non_positive_repeat(tmp_path, monkeypatch):
    async def fake_run_case(d, llm, settings):  # pragma: no cover - never reached
        raise AssertionError

    _patch_main(monkeypatch, tmp_path, ["--repeat", "0"], fake_run_case)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(run_evals.main())
    assert exc.value.code == 2
