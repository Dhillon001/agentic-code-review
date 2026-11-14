"""Unified-diff parser: `git diff`/`git format-patch` output, GitHub's .diff media type, and plain
`diff -u` / `diff -ru` output (files introduced by `---`/`+++` headers without a `diff --git` line)."""

from __future__ import annotations

import re

from reviewer.models import FileDiff, Hunk

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _header_path(raw: str) -> str:
    """Path from a `--- `/`+++ ` header: drop a `diff -u` timestamp (after a tab) and the a/ b/ prefix."""
    p = raw[4:].split("\t", 1)[0].strip()
    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
        p = p[1:-1]
    return p[2:] if p.startswith(("a/", "b/")) else p


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_line = 0
    old_left = new_left = 0  # lines the current hunk header promised; the hunk ends when both reach 0

    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if raw.startswith("diff --git "):
            rest = raw[len("diff --git ") :]
            a, _, b = rest.partition(" b/")
            old = a[2:] if a.startswith("a/") else a
            cur = FileDiff(path=b or old, old_path=old)
            files.append(cur)
            hunk = None
            continue

        # Once a hunk has all the lines its header promised, trailer text (a `diff -ru` "Only in" line,
        # a format-patch "-- " signature, its trailing space possibly stripped) ends it. Lines that still look like diff lines are kept, so a
        # hand-edited patch with an under-counted header doesn't lose its last lines.
        done = hunk is not None and old_left <= 0 and new_left <= 0
        if done and (raw.rstrip() == "--" or not raw.startswith((" ", "+", "-", "\\"))):
            hunk = None
            done = False

        # Plain `diff -u` file header: `--- old` immediately followed by `+++ new`, outside any hunk. A
        # `diff --git` file that has no hunks yet is still reading its own headers, so it isn't one.
        if (
            (hunk is None or done)
            and raw.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
            and (cur is None or cur.hunks)
        ):
            old, new = _header_path(raw), _header_path(lines[i + 1])
            if old == "/dev/null":
                cur = FileDiff(path=new, status="added")
            elif new == "/dev/null":
                cur = FileDiff(path=old, old_path=old, status="deleted")
            else:
                cur = FileDiff(path=new, old_path=old)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue

        # File headers only appear before the first hunk; inside a hunk a line like
        # "--- foo" is a removed line whose content starts with "-- ".
        if hunk is None:
            if raw.startswith("new file mode"):
                cur.status = "added"
                continue
            if raw.startswith("deleted file mode"):
                cur.status = "deleted"
                continue
            if raw.startswith("rename from"):
                cur.status = "renamed"
                continue
            if raw.startswith("+++ "):
                p = _header_path(raw)
                if p != "/dev/null":
                    cur.path = p
                continue
            if raw.startswith("--- "):
                continue
            if raw.startswith(("Binary files ", "GIT binary patch")):
                cur.binary = True
                continue

        m = HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(3))
            old_left = int(m.group(2)) if m.group(2) is not None else 1
            new_left = int(m.group(4)) if m.group(4) is not None else 1
            hunk = Hunk(header=raw, new_start=new_line)
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            continue

        hunk.lines.append(raw)
        if raw.startswith("+"):
            hunk.added_lines.append(new_line)
            new_line += 1
            new_left -= 1
        elif raw.startswith("-"):
            old_left -= 1
        elif raw.startswith("\\"):
            pass
        else:  # context (some tools strip the leading space on blank lines)
            hunk.context_lines.append(new_line)
            new_line += 1
            old_left -= 1
            new_left -= 1

    return files


def _span(h: Hunk) -> tuple[int, int]:
    """New-file line range a hunk touches. A pure deletion still marks the spot where lines went."""
    n = len(h.added_lines) + len(h.context_lines)
    return h.new_start, h.new_start + max(n, 1) - 1


def restrict_to_changes(pr_files: list[FileDiff], changed: list[FileDiff]) -> list[FileDiff]:
    """Keep only the PR hunks that overlap a hunk in `changed` (e.g. the diff of new commits).

    We filter the *PR* diff rather than reviewing `changed` directly because GitHub only accepts inline
    comments on lines in the PR diff; a hunk from the commit range alone may not be commentable."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for fd in changed:
        spans.setdefault(fd.path, []).extend(_span(h) for h in fd.hunks)
    out: list[FileDiff] = []
    for fd in pr_files:
        ranges = spans.get(fd.path)
        if not ranges:
            continue
        hunks = [
            h for h in fd.hunks
            if any(a <= _span(h)[1] and _span(h)[0] <= b for a, b in ranges)
        ]
        if hunks:
            out.append(fd.model_copy(update={"hunks": hunks}))
    return out
