"""Language hints: a short, language-specific pitfalls checklist added to the specialist prompt.

The data lives in one table (`LANGUAGES`); a file's language is detected from its extension. Hints are
computed per batch from the batch's file paths, so every specialist reviewing one batch still gets a
byte-identical system prompt (prompt caching keeps hitting).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

LANGUAGE_HINTS_HEADER = (
    "Language-specific pitfalls worth checking in the changed lines (a checklist, not a quota: report one "
    "only where the code shown actually has it, and only if it fits your focus):"
)


@dataclass(frozen=True)
class Language:
    name: str
    extensions: tuple[str, ...]  # lowercase, with the leading dot
    hints: tuple[str, ...]


LANGUAGES: tuple[Language, ...] = (
    Language(
        "Python",
        (".py", ".pyi", ".pyw"),
        (
            "mutable default arguments (`def f(x=[])`, `={}`) shared across calls",
            "async: blocking calls (`time.sleep`, `requests`, sync file/DB IO) inside `async def`; coroutines "
            "never awaited; `asyncio.create_task` results dropped (task garbage-collected, errors lost)",
            "bare/broad `except` that swallows errors (incl. `CancelledError`/`KeyboardInterrupt`)",
            "late-binding closures in loops (`lambda: i`) and mutating a list/dict while iterating it",
            "`is` used for value comparison; truthiness checks that treat `0`/`''` like `None`",
            "SQL built with f-strings, `%` or `+` instead of driver parameters",
        ),
    ),
    Language(
        "JavaScript/TypeScript",
        (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
        (
            "promises not awaited or returned (floating promises, unhandled rejections); `async` callbacks "
            "passed to `forEach`/`map` without `await Promise.all`",
            "`==`/`!=` coercion bugs; `||` defaults that clobber `0`/`''`/`false` (use `??`)",
            "`this` lost in callbacks; stale closures over loop or React state",
            "TypeScript `as`/`!` assertions or `any` hiding a real `undefined`/`null`",
            "SQL or shell commands built with template literals or `+` from user input; `innerHTML`/"
            "`dangerouslySetInnerHTML` with untrusted data",
        ),
    ),
    Language(
        "Go",
        (".go",),
        (
            "errors ignored (`_ =`, unchecked returns) or shadowed with `:=` in an inner scope",
            "goroutine leaks: sends/receives that can block forever, no `context` cancellation or `select` exit",
            "`defer` inside loops (resources held until the function returns); `defer` before the error check",
            "loop variable captured by goroutines/closures (pre-Go 1.22 semantics); data races on shared maps",
            "nil map writes, nil pointer/interface derefs; unclosed `resp.Body`/`rows`",
            "SQL built with `fmt.Sprintf` instead of query placeholders",
        ),
    ),
    Language(
        "Java",
        (".java",),
        (
            "null dereferences: unchecked return values, `Optional.get()` without a check, unboxing a null",
            "resources (streams, connections, statements) not closed — use try-with-resources",
            "`==` on strings/boxed numbers instead of `.equals`; `equals` overridden without `hashCode`",
            "swallowed exceptions (empty `catch`, catching `Exception`/`Throwable`); unsynchronized shared state",
            "SQL built by string concatenation into `Statement` instead of `PreparedStatement` parameters",
        ),
    ),
    Language(
        "SQL",
        (".sql",),
        (
            "dynamic SQL (`EXECUTE`/`EXEC`/`sp_executesql`) concatenating parameters — injection",
            "`UPDATE`/`DELETE` with a missing or too-broad `WHERE`",
            "`NULL` comparisons with `=`/`<>` and `NOT IN` over a nullable subquery",
            "migrations that lock or rewrite large tables, or are not reversible",
        ),
    ),
)

_BY_EXTENSION: dict[str, Language] = {ext: lang for lang in LANGUAGES for ext in lang.extensions}


def detect_language(path: str) -> Language | None:
    """The language for a file path, by extension (case-insensitive); None when unknown."""
    return _BY_EXTENSION.get(PurePosixPath(path).suffix.lower())


def language_hints(paths: list[str]) -> str:
    """Hint sections for every language present in `paths`, in table order; "" when none is known.

    Deterministic for a given set of paths (order and duplicates don't matter)."""
    files: dict[str, list[str]] = {}
    for p in sorted(set(paths)):
        lang = detect_language(p)
        if lang is not None:
            files.setdefault(lang.name, []).append(p)
    sections = []
    for lang in LANGUAGES:
        if lang.name in files:
            bullets = "\n".join(f"- {h}" for h in lang.hints)
            sections.append(f"{lang.name} ({', '.join(files[lang.name])}):\n{bullets}")
    if not sections:
        return ""
    return "\n".join([LANGUAGE_HINTS_HEADER, *sections])
