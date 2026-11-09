import json

import pytest

from reviewer import cli
from reviewer.gate import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK
from reviewer.models import Finding, ReviewResult, Severity
from reviewer.output.markdown import format_markdown
from reviewer.usage import Usage


def mk(sev: str, file: str = "a.py", line: int = 1, **kw) -> Finding:
    base = dict(file=file, line=line, severity=Severity(sev), category="bug", title=f"{sev} issue",
                detail=f"detail for {file}:{line}", agent="correctness", confidence=0.8)
    base.update(kw)
    return Finding(**base)


RESULT = ReviewResult(
    inline=[
        mk("medium", "b.py", 10),
        mk("low", "a.py", 30),
        mk("critical", "a.py", 5, category="sql-injection", verified=True,
           suggestion="Use a parameterised query.", replacement='cur.execute("SELECT * FROM t WHERE id = ?", (i,))'),
    ],
    summary_only=[mk("high", "b.py", 200, suggestion="Close the file.")],
    rejected=[mk("critical", "c.py", 1, title="rejected one")],
    stats={"raw_findings": 9, "candidates": 5, "confirmed": 4, "rejected": 1, "suppressed": 2},
    usage=Usage(calls=6, input_tokens=48_200, output_tokens=3_400, est_usd=0.21, wall_time_s=38.4),
)


def test_groups_by_file_most_severe_file_first_then_by_line():
    md = format_markdown(RESULT)
    assert md.startswith("# 🤖 Agentic review\n")
    a, b = md.index("## `a.py`"), md.index("## `b.py`")
    assert a < b  # a.py holds the critical
    assert md.index("Critical · sql-injection") < md.index("Low · bug") < b
    assert b < md.index("Medium · bug") < md.index("High · bug")  # b.py: line 10 before line 200


def test_header_tally_and_icons():
    md = format_markdown(RESULT)
    assert "**4 findings:** 🛑 1 critical · 🔴 1 high · 🟠 1 medium · 🟡 1 low" in md
    assert "### 🛑 Critical · sql-injection — critical issue" in md


def test_suggestion_block_and_prose_fix():
    md = format_markdown(RESULT)
    assert "**Suggested change:** Use a parameterised query." in md
    assert '```suggestion\ncur.execute("SELECT * FROM t WHERE id = ?", (i,))\n```' in md
    assert "**Suggested fix:** Close the file." in md


def test_off_diff_findings_marked_and_rejected_excluded():
    md = format_markdown(RESULT)
    assert "line 200 · outside the diff" in md
    assert "line 10 · outside" not in md
    assert "rejected one" not in md and "c.py" not in md


def test_footer_has_funnel_suppressed_and_usage():
    md = format_markdown(RESULT)
    footer = md.split("\n---\n", 1)[1]
    assert "9 raw → 5 candidates → 4 verified · 1 rejected by verifier · 2 suppressed" in footer
    assert "💸 6 calls · 48.2k in · 3.4k out · ~$0.21 · 38.4s" in footer
    assert "correctness · confidence 80% · verified" in md


def test_empty_result():
    md = format_markdown(ReviewResult())
    assert "✅ No issues survived verification." in md
    assert "##" not in md
    assert "💸" not in md  # no calls, no usage line


def test_odd_path_with_backticks_stays_a_code_span():
    md = format_markdown(ReviewResult(inline=[mk("low", "we`ird.py")]))
    assert "## ``we`ird.py``" in md


# ---------------------------------------------------------------- CLI wiring


@pytest.fixture
def fake_review(monkeypatch):
    async def fake_review_local(repo, base, head, settings):
        return RESULT

    monkeypatch.setattr(cli, "review_local", fake_review_local)


def test_cli_format_markdown_to_stdout(fake_review, capsys):
    assert cli.run(["local", "--format", "markdown"]) == EXIT_OK
    assert capsys.readouterr().out == format_markdown(RESULT)


def test_cli_json_alias_and_format_json_match(fake_review, capsys):
    cli.run(["local", "--json"])
    alias = capsys.readouterr().out
    cli.run(["local", "--format", "json"])
    assert capsys.readouterr().out == alias
    assert json.loads(alias)["stats"]["raw_findings"] == 9


def test_cli_default_is_text(fake_review, capsys):
    cli.run(["local"])
    out = capsys.readouterr().out
    assert "stats:" in out and "usage:" in out and "##" not in out


def test_cli_json_conflicts_with_other_format(fake_review, capsys):
    with pytest.raises(SystemExit) as e:
        cli.run(["local", "--json", "--format", "markdown"])
    assert e.value.code == 2
    assert "conflicts" in capsys.readouterr().err


def test_cli_out_writes_file_and_keeps_gate(fake_review, tmp_path, capsys):
    out = tmp_path / "review.md"
    assert cli.run(["local", "--format", "markdown", "--out", str(out), "--fail-on", "high"]) == EXIT_FINDINGS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "at or above 'high'" in captured.err
    assert out.read_text(encoding="utf-8") == format_markdown(RESULT)


def test_cli_out_unwritable_path_is_an_error(fake_review, tmp_path, capsys):
    assert cli.run(["local", "--out", str(tmp_path / "missing" / "r.txt")]) == EXIT_ERROR
    assert "cannot write report" in capsys.readouterr().err
