"""Score the reviewer against labelled cases.

Each case lives in evals/cases/<id>/:
    case.json     {"title": ..., "expected": [{"file", "line", "category"}]}   (empty list = clean PR)
    change.diff   unified diff
    files/...     post-change contents of every file the diff touches (plus any related files)

A finding matches an expected bug if it's in the same file within ±LINE_TOLERANCE lines.
Clean cases measure false positives — that's the number that decides whether people mute the bot.

    python evals/run_evals.py [--no-verify] [--only sql-injection] [--cache-dir .acr-cache]
                              [--report-md eval.md] [--baseline baseline.json [--tolerance 0.02]]

With `--cache-dir` (or `LLM_CACHE_DIR`) model responses are cached on disk, so rerunning unchanged cases
costs nothing and gives identical results.

Precision and recall are also broken down per category: recall for a category counts the expected bugs
labelled with it, precision counts the findings the reviewer filed under it. `--baseline` compares against
a previous report.json and exits 1 when overall F1 drops by more than `--tolerance`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
DEFAULT_TOLERANCE = 0.02


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
    return {
        "tp": len(matched_exp), "fn": len(expected) - len(matched_exp), "fp": len(found) - len(tp_findings),
        # Per-item outcomes, so results can be broken down by category afterwards.
        "expected_hits": [{"category": e["category"], "hit": i in matched_exp} for i, e in enumerate(expected)],
        "found_hits": [{"category": f.category, "hit": j in tp_findings} for j, f in enumerate(found)],
    }


def prf(tp: int, fp: int, fn: int) -> dict:
    """Precision/recall/F1. With nothing to find and nothing found, both are vacuously perfect."""
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def category_breakdown(rows: list[dict]) -> dict[str, dict]:
    """Per-category scores. Recall uses the expected labels' categories; precision uses the findings' own.

    A finding that lands on a labelled bug under a different category still counts as a hit (matching is
    by location), but it's credited to the category the reviewer chose, so mislabelling shows up as one
    category's precision and another's recall rather than vanishing.
    """
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for e in r.get("expected_hits", []):
            c = counts.setdefault(e["category"], {"tp": 0, "fp": 0, "fn": 0, "found_tp": 0})
            c["tp" if e["hit"] else "fn"] += 1
        for f in r.get("found_hits", []):
            c = counts.setdefault(f["category"], {"tp": 0, "fp": 0, "fn": 0, "found_tp": 0})
            c["found_tp" if f["hit"] else "fp"] += 1
    out = {}
    for cat in sorted(counts):
        c = counts[cat]
        precision = c["found_tp"] / (c["found_tp"] + c["fp"]) if c["found_tp"] + c["fp"] else 1.0
        recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[cat] = {"tp": c["tp"], "fp": c["fp"], "fn": c["fn"],
                    "precision": precision, "recall": recall, "f1": f1}
    return out


def build_report(rows: list[dict], verify: bool) -> dict:
    tp, fp, fn = (sum(r[k] for r in rows) for k in ("tp", "fp", "fn"))
    return {**prf(tp, fp, fn), "tp": tp, "fp": fp, "fn": fn, "verify": verify,
            "categories": category_breakdown(rows), "cases": rows}


def render_markdown(report: dict) -> str:
    lines = [
        "# Eval report", "",
        f"**precision** {report['precision']:.2f} · **recall** {report['recall']:.2f} · "
        f"**F1** {report['f1']:.2f} · verifier {'on' if report['verify'] else 'off'}", "",
        "## By category", "",
        "| Category | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cat, c in report["categories"].items():
        lines.append(f"| `{cat}` | {c['tp']} | {c['fp']} | {c['fn']} | "
                     f"{c['precision']:.2f} | {c['recall']:.2f} | {c['f1']:.2f} |")
    lines += ["", "## Cases", "", "| | Case | TP | FP | FN | Findings |", "|---|---|---:|---:|---:|---|"]
    for r in report["cases"]:
        status = "✅" if r["fn"] == 0 and r["fp"] == 0 else "❌"
        found = "<br>".join(f"`{f}`".replace("|", "\\|") for f in r.get("findings", [])) or "—"
        lines.append(f"| {status} | `{r['case']}` | {r['tp']} | {r['fp']} | {r['fn']} | {found} |")
    return "\n".join(lines) + "\n"


def compare_baseline(report: dict, baseline: dict, tolerance: float = DEFAULT_TOLERANCE) -> tuple[list[str], bool]:
    """Lines describing metric deltas vs. a baseline report, and whether F1 regressed beyond tolerance."""
    def delta(name: str, now: float, then: float) -> str:
        return f"{name:<24} {then:.2f} → {now:.2f}  ({now - then:+.2f})"

    lines = [delta(m, report[m], baseline.get(m, 0.0)) for m in ("precision", "recall", "f1")]
    base_cats = baseline.get("categories", {})
    for cat in sorted(set(report.get("categories", {})) | set(base_cats)):
        now = report.get("categories", {}).get(cat, {}).get("f1")
        then = base_cats.get(cat, {}).get("f1")
        if now is None or then is None:
            lines.append(f"{cat + ' f1':<24} {'new' if then is None else 'gone'}")
        else:
            lines.append(delta(f"{cat} f1", now, then))
    drop = baseline.get("f1", 0.0) - report["f1"]
    regressed = drop > tolerance + 1e-9
    lines.append(f"F1 dropped by {drop:.2f} (tolerance {tolerance:.2f}) — REGRESSION" if regressed
                 else f"F1 within tolerance ({tolerance:.2f})")
    return lines, regressed


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
        "calls": result.usage.calls,
        "response_cache_hits": result.usage.response_cache_hits,
        "est_usd": result.usage.est_usd,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true", help="measure the verifier's value by turning it off")
    ap.add_argument("--only")
    ap.add_argument("--cache-dir", help="cache model responses here (default: LLM_CACHE_DIR)")
    ap.add_argument("--report-md", metavar="PATH", help="also write a Markdown report here")
    ap.add_argument("--baseline", metavar="REPORT_JSON", help="compare against a previous report.json")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                    help=f"allowed F1 drop vs. --baseline before exiting 1 (default {DEFAULT_TOLERANCE})")
    args = ap.parse_args()

    # Read the baseline up front: it may well be the evals/report.json this run is about to overwrite.
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None

    update: dict = {"verify": not args.no_verify}
    if args.cache_dir:
        update["llm_cache_dir"] = args.cache_dir
    settings = get_settings().model_copy(update=update)
    llm = make_llm(settings)
    dirs = sorted(p for p in CASES.iterdir() if p.is_dir() and (not args.only or p.name == args.only))
    rows = await asyncio.gather(*(run_case(d, llm, settings) for d in dirs))
    report = build_report(list(rows), settings.verify)

    for r in rows:
        status = "✅" if r["fn"] == 0 and r["fp"] == 0 else "❌"
        print(f"{status} {r['case']:<28} tp={r['tp']} fp={r['fp']} fn={r['fn']}  (verifier rejected {r['rejected_by_verifier']})")
        for line in r["findings"]:
            print(f"     {line}")
    print("\nby category:")
    for cat, c in report["categories"].items():
        print(f"  {cat:<22} precision={c['precision']:.2f} recall={c['recall']:.2f} f1={c['f1']:.2f}"
              f"  (tp={c['tp']} fp={c['fp']} fn={c['fn']})")
    print(f"\nprecision={report['precision']:.2f} recall={report['recall']:.2f} f1={report['f1']:.2f}  verify={settings.verify}")
    if settings.llm_cache_dir:
        hits = sum(r["response_cache_hits"] for r in rows)
        calls = sum(r["calls"] for r in rows)
        print(f"model calls={calls}  response cache hits={hits}  ~${sum(r['est_usd'] for r in rows):.2f}")

    (Path(__file__).parent / "report.json").write_text(json.dumps(report, indent=2))
    if args.report_md:
        Path(args.report_md).write_text(render_markdown(report))

    if baseline is not None:
        lines, regressed = compare_baseline(report, baseline, args.tolerance)
        print(f"\nvs. baseline {args.baseline}:")
        for line in lines:
            print(f"  {line}")
        return 1 if regressed else 0
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
