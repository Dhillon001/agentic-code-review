"""Score the reviewer against labelled cases.

Each case lives in evals/cases/<id>/:
    case.json     {"title": ..., "expected": [{"file", "line", "category"}]}   (empty list = clean PR)
    change.diff   unified diff
    files/...     post-change contents of every file the diff touches (plus any related files)

A finding matches an expected bug if it's in the same file within ±LINE_TOLERANCE lines.
Clean cases measure false positives — that's the number that decides whether people mute the bot.

    python evals/run_evals.py [--no-verify] [--only sql-injection]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from reviewer.config import get_settings
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import Finding, PRContext
from reviewer.pipeline import review
from reviewer.service import make_llm

CASES = Path(__file__).parent / "cases"
LINE_TOLERANCE = 3


def load_case(d: Path) -> tuple[dict, str, dict[str, str]]:
    meta = json.loads((d / "case.json").read_text())
    files_dir = d / "files"
    files = {str(p.relative_to(files_dir)): p.read_text() for p in files_dir.rglob("*") if p.is_file()}
    return meta, (d / "change.diff").read_text(), files


def score(expected: list[dict], found: list[Finding]) -> dict:
    matched_exp, tp_findings = set(), set()
    for i, e in enumerate(expected):
        for j, f in enumerate(found):
            if f.file == e["file"] and abs(f.line - e["line"]) <= LINE_TOLERANCE:
                matched_exp.add(i)
                tp_findings.add(j)
    return {"tp": len(matched_exp), "fn": len(expected) - len(matched_exp), "fp": len(found) - len(tp_findings)}


async def run_case(d: Path, llm, settings) -> dict:
    meta, diff, files = load_case(d)
    source = InMemorySource(files)
    pr = PRContext(repo=f"eval/{d.name}", title=meta.get("title", ""),
                   files=await build_context(source, parse_unified_diff(diff), settings))
    result = await review(pr, llm, settings)
    found = result.inline + result.summary_only
    s = score(meta["expected"], found)
    return {
        "case": d.name, **s,
        "findings": [f"{f.file}:{f.line} [{f.severity.value}/{f.category}] {f.title}" for f in found],
        "rejected_by_verifier": len(result.rejected),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true", help="measure the verifier's value by turning it off")
    ap.add_argument("--only")
    args = ap.parse_args()

    settings = get_settings().model_copy(update={"verify": not args.no_verify})
    llm = make_llm(settings)
    dirs = sorted(p for p in CASES.iterdir() if p.is_dir() and (not args.only or p.name == args.only))
    rows = await asyncio.gather(*(run_case(d, llm, settings) for d in dirs))

    tp, fp, fn = (sum(r[k] for r in rows) for k in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    for r in rows:
        status = "✅" if r["fn"] == 0 and r["fp"] == 0 else "❌"
        print(f"{status} {r['case']:<28} tp={r['tp']} fp={r['fp']} fn={r['fn']}  (verifier rejected {r['rejected_by_verifier']})")
        for line in r["findings"]:
            print(f"     {line}")
    print(f"\nprecision={precision:.2f} recall={recall:.2f} f1={f1:.2f}  verify={settings.verify}")

    report = {"precision": precision, "recall": recall, "f1": f1, "verify": settings.verify, "cases": rows}
    (Path(__file__).parent / "report.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
