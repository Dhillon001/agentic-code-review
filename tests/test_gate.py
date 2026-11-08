import pytest

from reviewer import cli
from reviewer.gate import (
    EXIT_ERROR,
    EXIT_FINDINGS,
    EXIT_OK,
    SEVERITY_CHOICES,
    blocking_findings,
    gate_exit_code,
    gate_message,
)
from reviewer.models import Finding, ReviewResult, Severity


def mk(sev: str, line: int = 1, conf: float = 0.8, file: str = "a.py") -> Finding:
    return Finding(file=file, line=line, severity=Severity(sev), category="bug", title=f"{sev} issue",
                   detail="d", confidence=conf)


RESULT = ReviewResult(inline=[mk("medium", 3)], summary_only=[mk("high", 40)], rejected=[mk("critical", 9)])


def test_choices_ordered_low_to_critical():
    assert SEVERITY_CHOICES == ["low", "medium", "high", "critical"]


@pytest.mark.parametrize(
    "fail_on, expected",
    [(None, EXIT_OK), ("critical", EXIT_OK), ("high", EXIT_FINDINGS), ("medium", EXIT_FINDINGS), ("low", EXIT_FINDINGS)],
)
def test_exit_code_by_threshold(fail_on, expected):
    assert gate_exit_code(RESULT, fail_on) == expected


def test_rejected_findings_never_fail_the_build():
    only_rejected = ReviewResult(rejected=[mk("critical")])
    assert gate_exit_code(only_rejected, "low") == EXIT_OK


def test_summary_only_findings_count():
    assert [f.line for f in blocking_findings(RESULT, "high")] == [40]


def test_blocking_sorted_most_severe_first():
    r = ReviewResult(inline=[mk("medium", 1), mk("critical", 2), mk("high", 3, conf=0.9), mk("high", 4, conf=0.6)])
    assert [f.line for f in blocking_findings(r, "medium")] == [2, 3, 4, 1]


def test_gate_message():
    assert gate_message(RESULT, None) is None
    assert gate_message(RESULT, "critical") is None
    msg = gate_message(RESULT, "medium")
    assert msg.startswith("acr: 2 findings at or above 'medium'")
    assert "a.py:40" in msg  # worst one named


# ---------------------------------------------------------------- CLI wiring


def _patch_review(monkeypatch, result=None, exc=None):
    async def fake_review_local(repo, base, head, settings):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(cli, "review_local", fake_review_local)


def test_cli_exit_1_when_gate_trips(monkeypatch, capsys):
    _patch_review(monkeypatch, RESULT)
    assert cli.run(["local", "--fail-on", "high"]) == EXIT_FINDINGS
    assert "at or above 'high'" in capsys.readouterr().err


def test_cli_exit_0_below_threshold_and_without_flag(monkeypatch):
    _patch_review(monkeypatch, RESULT)
    assert cli.run(["local", "--fail-on", "critical"]) == EXIT_OK
    assert cli.run(["local"]) == EXIT_OK


def test_cli_json_output_still_gated(monkeypatch, capsys):
    _patch_review(monkeypatch, RESULT)
    assert cli.run(["local", "--json", "--fail-on", "medium"]) == EXIT_FINDINGS
    assert '"inline"' in capsys.readouterr().out


def test_cli_exit_2_on_review_error(monkeypatch, capsys):
    _patch_review(monkeypatch, exc=RuntimeError("API overloaded"))
    assert cli.run(["local", "--fail-on", "high"]) == EXIT_ERROR
    assert "review failed: RuntimeError: API overloaded" in capsys.readouterr().err


def test_cli_exit_2_on_config_error(monkeypatch, capsys):
    _patch_review(monkeypatch, exc=SystemExit("ANTHROPIC_API_KEY is not set"))
    assert cli.run(["local"]) == EXIT_ERROR
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_cli_rejects_unknown_severity(monkeypatch):
    _patch_review(monkeypatch, RESULT)
    with pytest.raises(SystemExit) as exc:
        cli.run(["local", "--fail-on", "urgent"])
    assert exc.value.code == 2  # argparse usage error


def test_main_exits_with_run_code(monkeypatch):
    _patch_review(monkeypatch, RESULT)
    monkeypatch.setattr("sys.argv", ["acr", "local", "--fail-on", "low"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == EXIT_FINDINGS
