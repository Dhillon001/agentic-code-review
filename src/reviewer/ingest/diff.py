"""Unified-diff parser (works on `git diff` output and GitHub's .diff media type)."""

import re

from reviewer.models import FileDiff, Hunk

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    new_line = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            rest = raw[len("diff --git ") :]
            a, _, b = rest.partition(" b/")
            old = a[2:] if a.startswith("a/") else a
            cur = FileDiff(path=b or old, old_path=old)
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
                p = raw[4:].strip()
                if p != "/dev/null":
                    cur.path = p[2:] if p.startswith("b/") else p
                continue
            if raw.startswith("--- "):
                continue

        m = HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(3))
            hunk = Hunk(header=raw, new_start=new_line)
            cur.hunks.append(hunk)
            continue
        if hunk is None:
            continue

        hunk.lines.append(raw)
        if raw.startswith("+"):
            hunk.added_lines.append(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            pass
        else:  # context (some tools strip the leading space on blank lines)
            hunk.context_lines.append(new_line)
            new_line += 1

    return files
