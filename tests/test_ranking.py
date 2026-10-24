from reviewer.ingest.diff import parse_unified_diff
from reviewer.models import FileContext, Finding, Severity
from reviewer.ranking import dedupe, place, rank


def mk(line, sev="high", cat="correctness", conf=0.8, file="a.py"):
    return Finding(file=file, line=line, severity=Severity(sev), category=cat, title="t", detail="d", confidence=conf)


def test_dedupe_keeps_highest():
    out = dedupe([mk(10, conf=0.6), mk(11, conf=0.9), mk(30)])
    assert sorted(f.line for f in out) == [11, 30]


def test_dedupe_cross_agent_same_line():
    out = dedupe([mk(10, cat="security"), mk(10, cat="correctness", sev="medium")])
    assert len(out) == 1 and out[0].category == "security"


def test_rank_filters_and_caps():
    fs = [mk(1, "low"), mk(2, "medium"), mk(3, "critical"), mk(4, "high")]
    out = rank(fs, "medium", max_comments=2)
    assert [f.severity.value for f in out] == ["critical", "high"]


def test_place_snaps_or_summarises():
    diff = parse_unified_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n x\n+y\n z\n")[0]
    files = {"a.py": FileContext(diff=diff)}
    inline, summary = place([mk(2), mk(5), mk(40)], files)
    assert [f.line for f in inline] == [2, 3]  # 5 snaps to 3
    assert [f.line for f in summary] == [40]
