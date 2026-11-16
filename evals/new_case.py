"""Scaffold an eval case from a commit in a local git repository.

    python evals/new_case.py --repo PATH --commit SHA --name NAME [--title TEXT] [--include PATH ...]

Writes evals/cases/<NAME>/ in the format run_evals.py reads:

    change.diff   the commit's unified diff against its parent (the empty tree for a root commit)
    files/...     post-change contents of every file the diff touches, plus any --include files
    case.json     title (default: the commit subject), the source commit, and an `expected` stub

The stub holds one placeholder entry with category "TODO" and a `_todo` note listing each file's
commentable lines. Replace the placeholder with the real {file, line, category} bugs — or empty the list
for a clean case — then delete `_todo`. `pytest` fails while any case still has a TODO entry.

Binary and non-UTF-8 files are left out of both the diff and files/ (there's nothing to review in them).
Merge commits are refused. An existing case directory is never overwritten.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from reviewer.ingest.diff import parse_unified_diff

CASES = Path(__file__).parent / "cases"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
TODO = "TODO"


class CaseError(Exception):
    """The case can't be built (bad name, bad commit, already exists, nothing to review)."""


def _git(repo: Path, *args: str) -> bytes:
    out = subprocess.run(
        # Pin the diff format so user config (noprefix, mnemonic prefixes, external diff) can't change it.
        ["git", "-C", str(repo), "-c", "core.quotepath=off", "-c", "diff.noprefix=false",
         "-c", "diff.mnemonicPrefix=false", *args],
        capture_output=True, check=False,
    )
    if out.returncode != 0:
        raise CaseError(f"git {' '.join(args[:2])} failed: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


def _ranges(lines: set[int]) -> str:
    """{1, 2, 3, 7, 9, 10} -> "1-3, 7, 9-10"."""
    out: list[str] = []
    for n in sorted(lines):
        if out and n == int(out[-1].rpartition("-")[2]) + 1:
            out[-1] = f"{out[-1].partition('-')[0]}-{n}"
        else:
            out.append(str(n))
    return ", ".join(out)


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def build_case(repo: str | Path, commit: str, name: str, *, cases_dir: Path = CASES,
               title: str | None = None, include: tuple[str, ...] | list[str] = ()) -> Path:
    """Create `cases_dir/name` from `commit` in `repo`. Returns the new directory; raises CaseError."""
    if not NAME_RE.match(name):
        raise CaseError(f"invalid case name {name!r}: use letters, digits, '-' and '_' (e.g. sql-injection)")
    dest = cases_dir / name
    if dest.exists():
        raise CaseError(f"{dest} already exists; refusing to overwrite (pick another --name or delete it)")

    repo = Path(repo).resolve()
    sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").decode().strip()
    parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()[1:]
    if len(parents) > 1:
        raise CaseError(f"{sha[:12]} is a merge commit; scaffold from one of its non-merge commits instead")
    base = parents[0] if parents else _git(repo, "hash-object", "-t", "tree", "--stdin").decode().strip()

    def diff(*pathspec: str) -> str:
        args = ["diff", "--no-color", "--no-ext-diff", "-M", "-U3", base, sha]
        return _git(repo, *args, *(("--", *pathspec) if pathspec else ())).decode("utf-8", errors="replace")

    def show(path: str) -> bytes | None:
        try:
            return _git(repo, "show", f"{sha}:{path}")
        except CaseError:
            return None

    # Binary files, and text that isn't UTF-8, can't be reviewed or stored in files/: drop them from the diff.
    files: dict[str, str] = {}
    excluded: list[str] = []
    for fd in parse_unified_diff(diff()):
        if fd.status == "deleted":
            continue
        text = None if fd.binary else _decode(show(fd.path) or b"")
        if text is None:
            excluded += [p for p in (fd.path, fd.old_path) if p]
        else:
            files[fd.path] = text
    if excluded:
        change = diff(".", *(f":(exclude,literal){p}" for p in excluded))
    else:
        change = diff()
    diffs = parse_unified_diff(change)
    if not diffs:
        raise CaseError(f"{sha[:12]} has no reviewable text changes")

    for path in include:
        if path in files:
            continue
        data = show(path)
        if data is None:
            raise CaseError(f"--include {path}: not found at {sha[:12]}")
        text = _decode(data)
        if text is None:
            raise CaseError(f"--include {path}: binary or not UTF-8")
        files[path] = text

    commentable = {fd.path: fd.commentable_lines for fd in diffs if fd.commentable_lines}
    first = next(iter(commentable), diffs[0].path)
    first_line = min(commentable.get(first) or {1})
    subject = _git(repo, "log", "-1", "--format=%s", sha).decode().strip()
    meta = {
        "title": title or subject or name,
        "source": {"repo": repo.name, "commit": sha},
        "expected": [{"file": first, "line": first_line, "category": TODO}],
        "_todo": {
            "note": "Replace the TODO entry with one {file, line, category} per real bug (an empty list = a "
                    "clean case), then delete this _todo key. Lines must be commentable:",
            "commentable_lines": {p: _ranges(lines) for p, lines in commentable.items()},
            **({"excluded_binary_or_non_utf8": sorted(set(excluded))} if excluded else {}),
        },
    }

    # Everything is in memory; create the directory last so a failure leaves nothing half-written.
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "change.diff").write_text(change)
    for path, text in files.items():
        out = (dest / "files" / path).resolve()
        if not out.is_relative_to((dest / "files").resolve()):
            continue  # a path like ../x can't come from git, but never write outside the case
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    (dest / "case.json").write_text(json.dumps(meta, indent=2) + "\n")
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scaffold an eval case from a local git commit.")
    ap.add_argument("--repo", required=True, help="path to the git repository")
    ap.add_argument("--commit", required=True, help="commit to turn into a case (SHA, tag, HEAD~2, …)")
    ap.add_argument("--name", required=True, help="case directory name, e.g. missing-auth-check")
    ap.add_argument("--title", help="case title (default: the commit subject)")
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="also copy this file (at the commit) into files/ as context; repeatable")
    ap.add_argument("--cases-dir", type=Path, default=CASES, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        dest = build_case(args.repo, args.commit, args.name, cases_dir=args.cases_dir,
                          title=args.title, include=args.include)
    except CaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    meta = json.loads((dest / "case.json").read_text())
    print(f"created {dest}")
    for path, lines in meta["_todo"]["commentable_lines"].items():
        print(f"  {path}: commentable lines {lines}")
    for path in meta["_todo"].get("excluded_binary_or_non_utf8", []):
        print(f"  {path}: skipped (binary or not UTF-8)")
    print(f"next: edit {dest / 'case.json'} — replace the TODO entry with the real bugs (or [] for a clean "
          "case), delete _todo, then run `pytest tests/test_eval_cases.py`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
