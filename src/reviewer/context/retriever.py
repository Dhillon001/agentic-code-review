"""Context retrieval: give reviewers more than the raw diff.

Every changed file gets its content (whole, or windows around hunks) plus related snippets from a
`Retriever`. The default `LexicalRetriever` pulls call sites of symbols defined/changed in the diff;
`EmbeddingRetriever` (see `embedding.py`) ranks repo chunks by embedding similarity. Pick one with the
`retriever: lexical|embedding` setting, or pass any object with a matching `related()` to `build_context`.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath
from typing import Protocol

from reviewer.config import Settings
from reviewer.ingest.sources import Source
from reviewer.models import FileContext, FileDiff, RelatedSnippet

# Fallback for languages without their own patterns (Python, Rust, Ruby, ...).
SYMBOL_RE = re.compile(r"\b(?:def|class|function|func|fn|interface|struct|type)\s+([A-Za-z_]\w*)")

# Words that look like a call/definition head (`if (`, `return foo(`) but never name a symbol.
_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "catch", "try", "finally", "return", "throw", "new",
    "delete", "typeof", "instanceof", "await", "yield", "function", "constructor", "super", "this", "import",
    "export", "from", "default", "in", "of", "with", "when", "is", "as", "sizeof", "synchronized", "assert",
})

_JS_PATTERNS = [
    # function foo(  /  async function* foo(  /  export default function foo(
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\btype\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*="),
    # const foo = (a) =>  /  let foo = async x =>  /  var foo = function(  /  const foo: Fn = () =>
    re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
        r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"
    ),
    # class members (no nested parens, so `setTimeout(function () {` isn't one):  async foo(a) {  /  static get bar() {  /  private baz(x: T): R {  /  qux = (e) => {
    re.compile(
        r"^\s*(?:(?:public|private|protected|static|readonly|abstract|override|async|get|set|declare)\s+)*"
        r"#?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;()]*\)\s*(?::\s*[^{;=]+)?\{"
    ),
    re.compile(
        r"^\s*(?:(?:public|private|protected|static|readonly)\s+)*#?([A-Za-z_$][\w$]*)\s*="
        r"\s*(?:async\s+)?\([^)]*\)\s*=>"
    ),
]

_GO_PATTERNS = [
    # func Name(  /  func (r *T) Name(  /  func Name[T any](
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]"),
    re.compile(r"^\s*type\s+([A-Za-z_]\w*)\b"),
]

_JAVA_MODIFIERS = r"(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)"
_JVM_PATTERNS = [
    re.compile(r"\b(?:class|interface|enum|record|object)\s+([A-Za-z_]\w*)"),
    # Kotlin: fun name(  /  fun <T> List<T>.name(  /  suspend fun name(
    re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.<>?, ]+\.)?([A-Za-z_]\w*)\s*\("),
    # Java methods: [annotations] [modifiers] [<T>] ReturnType name(...)  — not a statement (no trailing `;`)
    re.compile(
        rf"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:{_JAVA_MODIFIERS}\s+)*(?:<[^>]*>\s+)?"
        r"([A-Za-z_][\w.$]*(?:<[^()]*>)?(?:\[\])*)\s+([A-Za-z_]\w*)\s*\([^;]*$"
    ),
    # Java constructors: modifiers are required so `foo(bar)` calls don't match
    re.compile(rf"^\s*(?:{_JAVA_MODIFIERS}\s+)+([A-Z]\w*)\s*\([^;]*$"),
]

_PATTERNS_BY_EXT: dict[str, list[re.Pattern[str]]] = {
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"), _JS_PATTERNS),
    ".go": _GO_PATTERNS,
    **dict.fromkeys((".java", ".kt", ".kts"), _JVM_PATTERNS),
}


def symbols_in_line(line: str, path: str) -> list[str]:
    """Names a single source line defines, using patterns for the file's language (by extension)."""
    patterns = _PATTERNS_BY_EXT.get(PurePosixPath(path).suffix.lower())
    if patterns is None:
        return SYMBOL_RE.findall(line)
    out: list[str] = []
    for pat in patterns:
        for m in pat.finditer(line):
            name = m.group(m.lastindex or 1)  # Java methods capture (return type, name)
            if m.lastindex and m.lastindex > 1 and m.group(1) in _KEYWORDS:
                continue  # `return foo(` / `new Foo(` / `throw err(`
            if name not in _KEYWORDS and name not in out:
                out.append(name)
    return out


def changed_symbols(diff: FileDiff) -> list[str]:
    """Symbols defined on added lines, plus enclosing definitions visible in the hunk
    (context lines and the `@@ ... @@ def foo():` header) — so body-only edits still find callers.
    JS/TS, Go and Java/Kotlin get language-aware patterns; other languages use `SYMBOL_RE`."""
    seen: dict[str, None] = {}
    for h in diff.hunks:
        if not h.added_lines:
            continue
        candidates = [h.header.split("@@")[-1]] + [ln[1:] for ln in h.lines if not ln.startswith("-")]
        for line in candidates:
            for name in symbols_in_line(line, diff.path):
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
