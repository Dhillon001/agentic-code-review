"""Context retrieval: give reviewers more than the raw diff.

Every changed file gets its content (whole, or windows around hunks) plus related snippets from a
`Retriever`. The default `LexicalRetriever` pulls call sites of symbols defined/changed in the diff;
`EmbeddingRetriever` (see `embedding.py`) ranks repo chunks by embedding similarity. Pick one with the
`retriever: lexical|embedding` setting, or pass any object with a matching `related()` to `build_context`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Protocol

from reviewer.config import Settings
from reviewer.ingest.sources import Source
from reviewer.models import FileContext, FileDiff, RelatedSnippet

SYMBOL_RE = re.compile(r"\b(?:def|class|function|func|fn|interface|struct|type)\s+([A-Za-z_]\w*)")


def changed_symbols(diff: FileDiff) -> list[str]:
    """Symbols defined on added lines, plus enclosing definitions visible in the hunk
    (context lines and the `@@ ... @@ def foo():` header) — so body-only edits still find callers."""
    seen: dict[str, None] = {}
    for h in diff.hunks:
        if not h.added_lines:
            continue
        candidates = [h.header.split("@@")[-1]] + [ln for ln in h.lines if not ln.startswith("-")]
        for line in candidates:
            for name in SYMBOL_RE.findall(line):
                if len(name) >= 4 and not name.startswith("__"):
                    seen.setdefault(name, None)
    return list(seen)


async def find_related(source: Source, diff: FileDiff, settings: Settings) -> list[RelatedSnippet]:
    out: list[RelatedSnippet] = []
    seen = {diff.path}
    for sym in changed_symbols(diff)[:3]:
        for path in await source.search(sym, limit=5):
            if path in seen:
                continue
            text = await source.read_file(path)
            if not text:
                continue
            lines = text.splitlines()
            pat = re.compile(rf"\b{re.escape(sym)}\b")
            idx = next((i for i, ln in enumerate(lines) if pat.search(ln)), None)
            if idx is None:
                continue
            start = max(0, idx - 5)
            window = lines[start : idx + 10]
            out.append(
                RelatedSnippet(
                    path=path,
                    reason=f"references `{sym}`",
                    start_line=start + 1,
                    text=number_lines(window, start + 1),
                )
            )
            seen.add(path)
            if len(out) >= settings.max_related_per_file:
                return out
    return out


class Retriever(Protocol):
    async def related(self, source: Source, diff: FileDiff, settings: Settings) -> list[RelatedSnippet]: ...


class LexicalRetriever:
    """Call sites of the changed symbols, found by code search. Cheap, precise, no model needed."""

    async def related(self, source: Source, diff: FileDiff, settings: Settings) -> list[RelatedSnippet]:
        return await find_related(source, diff, settings)


def make_retriever(settings: Settings) -> Retriever:
    if settings.retriever == "embedding":
        from reviewer.context.embedding import EmbeddingRetriever  # imports this module

        return EmbeddingRetriever(chunk_lines=settings.embedding_chunk_lines, max_files=settings.embedding_max_files)
    return LexicalRetriever()


async def build_context(
    source: Source, diffs: list[FileDiff], settings: Settings, retriever: Retriever | None = None
) -> list[FileContext]:
    """`retriever` defaults to the one `settings.retriever` names; inject one to use a custom embedder."""
    retriever = retriever or make_retriever(settings)
    sem = asyncio.Semaphore(8)

    async def one(d: FileDiff) -> FileContext:
        if d.status == "deleted" or not d.hunks:
            return FileContext(diff=d)
        async with sem:
            content = await source.read_file(d.path)
            related = await retriever.related(source, d, settings)
        return FileContext(diff=d, content=content, related=related)

    return list(await asyncio.gather(*(one(d) for d in diffs[: settings.max_files])))


# ---------------------------------------------------------------- rendering helpers


def number_lines(lines: list[str], start: int = 1) -> str:
    return "\n".join(f"{start + i:>5} | {ln}" for i, ln in enumerate(lines))


def render_file_excerpt(fc: FileContext, settings: Settings, focus_lines: set[int] | None = None) -> str:
    """Whole file if small; otherwise merged windows around changed (or focus) lines."""
    if not fc.content:
        return "(file content unavailable)"
    lines = fc.content.splitlines()
    if len(lines) <= settings.full_file_max_lines:
        return number_lines(lines)

    targets = focus_lines or fc.diff.commentable_lines
    k = settings.context_lines
    ranges: list[list[int]] = []
    for n in sorted(targets):
        lo, hi = max(1, n - k), min(len(lines), n + k)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    chunks = [number_lines(lines[lo - 1 : hi], lo) for lo, hi in ranges]
    return "\n  ...\n".join(chunks)
