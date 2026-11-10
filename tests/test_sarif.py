import json

import pytest

from reviewer import __version__, cli
from reviewer.gate import EXIT_FINDINGS, EXIT_OK
from reviewer.models import Finding, ReviewResult, Severity
from reviewer.output.sarif import FINGERPRINT_KEY, build_sarif, fingerprint, format_sarif, rule_id
from reviewer.usage import Usage

LEVELS = {"none", "note", "warning", "error"}


def mk(sev: str, file: str = "a.py", line: int = 1, **kw) -> Finding:
    base = dict(file=file, line=line, severity=Severity(sev), category="bug", title=f"{sev} issue",
                detail=f"detail for {file}:{line}", agent="correctness", confidence=0.8)
    base.update(kw)
    return Finding(**base)


RESULT = ReviewResult(
    inline=[
        mk("medium", "b.py", 10),
        mk("low", "a.py", 30),
        mk("critical", "src/db.py", 5, category="sql-injection", agent="security", verified=True,
           suggestion="Use a parameterised query.", replacement='cur.execute("SELECT * FROM t WHERE id = ?", (i,))'),
    ],
    summary_only=[mk("high", "b.py", 200, suggestion="Close the file.")],
    rejected=[mk("critical", "c.py", 1, title="rejected one")],
    stats={"raw_findings": 9, "candidates": 5, "confirmed": 4, "rejected": 1},
    usage=Usage(calls=6, input_tokens=48_200, output_tokens=3_400, est_usd=0.21, wall_time_s=38.4),
)


def validate_sarif(doc: dict) -> None:
    """The parts of the SARIF 2.1.0 schema (and GitHub's upload requirements) this tool relies on."""
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert isinstance(driver["name"], str) and driver["name"]
    rules = driver["rules"]
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids)), "rule ids must be unique"
    for r in rules:
        assert r["defaultConfiguration"]["level"] in LEVELS
        assert r["shortDescription"]["text"]
    for res in run["results"]:
        assert res["message"]["text"]
        assert res["level"] in LEVELS
        assert rules[res["ruleIndex"]]["id"] == res["ruleId"]
        (loc,) = res["locations"]
        phys = loc["physicalLocation"]
        assert phys["artifactLocation"]["uriBaseId"] == "%SRCROOT%"
        assert not phys["artifactLocation"]["uri"].startswith(("/", "./"))
        assert isinstance(phys["region"]["startLine"], int) and phys["region"]["startLine"] >= 1
        assert all(isinstance(v, str) and v for v in res["partialFingerprints"].values())
        for fix in res.get("fixes", []):
            for change in fix["artifactChanges"]:
                for rep in change["replacements"]:
                    assert rep["deletedRegion"]["startLine"] >= 1
                    assert "text" in rep["insertedContent"]
    json.dumps(doc)  # serialisable


def test_structure_is_valid_sarif():
    doc = build_sarif(RESULT)
    validate_sarif(doc)
    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "agentic-code-review" and driver["version"] == __version__


def test_one_rule_per_category_with_worst_level():
    rules = {r["id"]: r for r in build_sarif(RESULT)["runs"][0]["tool"]["driver"]["rules"]}
    assert set(rules) == {"bug", "sql-injection"}
    assert rules["bug"]["defaultConfiguration"]["level"] == "error"  # worst bug is high
    assert rules["sql-injection"]["properties"]["tags"] == ["security"]
    assert rules["sql-injection"]["properties"]["security-severity"] == "9.5"
    assert "security-severity" not in rules["bug"]["properties"]


def test_levels_locations_and_rejected_excluded():
    results = build_sarif(RESULT)["runs"][0]["results"]
    assert len(results) == 4  # rejected finding never emitted
    got = [(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            r["locations"][0]["physicalLocation"]["region"]["startLine"], r["level"]) for r in results]
    assert got == [("a.py", 30, "note"), ("b.py", 10, "warning"), ("b.py", 200, "error"), ("src/db.py", 5, "error")]
    off = {r["locations"][0]["physicalLocation"]["region"]["startLine"]: r["properties"]["outsideDiff"]
           for r in results}
    assert off[200] is True and off[10] is False
    assert "Suggested fix: Close the file." in results[2]["message"]["text"]


def test_single_line_replacement_becomes_fix():
    sql = build_sarif(RESULT)["runs"][0]["results"][3]
    (fix,) = sql["fixes"]
    (rep,) = fix["artifactChanges"][0]["replacements"]
    assert rep["deletedRegion"] == {"startLine": 5, "startColumn": 1, "endLine": 6, "endColumn": 1}
    assert rep["insertedContent"]["text"] == 'cur.execute("SELECT * FROM t WHERE id = ?", (i,))\n'
    assert fix["description"]["text"] == "Use a parameterised query."
    multi = build_sarif(ReviewResult(inline=[mk("low", replacement="a\nb")]))["runs"][0]["results"][0]
    assert "fixes" not in multi


def test_fingerprint_stable_across_line_moves_but_not_titles():
    a = mk("high", "x.py", 10, title="Unclosed file")
    assert fingerprint(a) == fingerprint(a.model_copy(update={"line": 42, "detail": "reworded"}))
    assert fingerprint(a) == fingerprint(a.model_copy(update={"title": "  unclosed   FILE "}))
    assert fingerprint(a) != fingerprint(a.model_copy(update={"title": "Leaked socket"}))
    assert fingerprint(a) != fingerprint(a.model_copy(update={"file": "y.py"}))
    res = build_sarif(ReviewResult(inline=[a]))["runs"][0]["results"][0]
    assert res["partialFingerprints"] == {FINGERPRINT_KEY: fingerprint(a)}


@pytest.mark.parametrize(
    "category,expected",
    [("sql-injection", "sql-injection"), ("Team Rule", "team-rule"), ("N+1 query", "n-1-query"), ("", "uncategorized")],
)
def test_rule_id_slugs(category, expected):
    assert rule_id(category) == expected


def test_odd_paths_are_uri_encoded():
    doc = build_sarif(ReviewResult(inline=[mk("low", "./docs/my file#1.md"), mk("low", "win\\path.py")]))
    validate_sarif(doc)
    uris = sorted(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in doc["runs"][0]["results"])
    assert uris == ["docs/my%20file%231.md", "win/path.py"]


def test_empty_result_is_valid_with_no_results():
    doc = build_sarif(ReviewResult())
    validate_sarif(doc)
    assert doc["runs"][0]["results"] == [] and doc["runs"][0]["tool"]["driver"]["rules"] == []
    assert "usage" not in doc["runs"][0]["properties"]


def test_run_properties_carry_stats_and_usage():
    props = build_sarif(RESULT)["runs"][0]["properties"]
    assert props["stats"]["raw_findings"] == 9
    assert props["usage"]["calls"] == 6


# ---------------------------------------------------------------- CLI wiring


@pytest.fixture
def fake_review(monkeypatch):
    async def fake_review_local(repo, base, head, settings):
        return RESULT

    monkeypatch.setattr(cli, "review_local", fake_review_local)


def test_cli_format_sarif_to_stdout(fake_review, capsys):
    assert cli.run(["local", "--format", "sarif"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out == format_sarif(RESULT)
    validate_sarif(json.loads(out))


def test_cli_sarif_out_with_gate(fake_review, tmp_path, capsys):
    path = tmp_path / "acr.sarif"
    assert cli.run(["local", "--format", "sarif", "--out", str(path), "--fail-on", "critical"]) == EXIT_FINDINGS
    assert capsys.readouterr().out == ""
    validate_sarif(json.loads(path.read_text(encoding="utf-8")))


def test_cli_json_conflicts_with_sarif(fake_review):
    with pytest.raises(SystemExit) as e:
        cli.run(["local", "--json", "--format", "sarif"])
    assert e.value.code == 2
