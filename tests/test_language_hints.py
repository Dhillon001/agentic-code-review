from __future__ import annotations

import pytest
from conftest import FakeLLM

from reviewer.agents.languages import LANGUAGE_HINTS_HEADER, LANGUAGES, detect_language, language_hints
from reviewer.agents.specialists import SPECIALISTS, reviewer_system
from reviewer.context.retriever import build_context
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource
from reviewer.models import PRContext
from reviewer.pipeline import review


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("app/models.py", "Python"),
        ("stubs/x.pyi", "Python"),
        ("web/src/App.TSX", "JavaScript/TypeScript"),
        ("web/index.mjs", "JavaScript/TypeScript"),
        ("lib/util.js", "JavaScript/TypeScript"),
        ("cmd/server/main.go", "Go"),
        ("src/main/java/com/acme/Repo.java", "Java"),
        ("db/migrations/001_init.sql", "SQL"),
        ("README.md", None),
        ("Makefile", None),
        ("go.mod", None),
    ],
)
def test_detect_language_by_extension(path, language):
    lang = detect_language(path)
    assert (lang.name if lang else None) == language


def test_table_is_well_formed():
    names = [lang.name for lang in LANGUAGES]
    assert len(names) == len(set(names))
    exts = [e for lang in LANGUAGES for e in lang.extensions]
    assert len(exts) == len(set(exts)), "an extension maps to two languages"
    for lang in LANGUAGES:
        assert lang.hints and all(h.strip() for h in lang.hints)
        assert all(e.startswith(".") and e == e.lower() for e in lang.extensions)


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("a.py", "mutable default"),
        ("a.py", "async"),
        ("a.ts", "promises not awaited"),
        ("a.js", "`==`"),
        ("a.go", "errors ignored"),
        ("a.go", "goroutine leaks"),
        ("A.java", "null dereferences"),
        ("A.java", "try-with-resources"),
        ("q.sql", "injection"),
    ],
)
def test_hints_cover_roadmap_pitfalls(path, needle):
    assert needle in language_hints([path])


def test_hints_only_for_languages_present_and_deterministic():
    text = language_hints(["b.go", "a.py", "c.md", "a.py"])
    assert text.startswith(LANGUAGE_HINTS_HEADER)
    assert "Python (a.py):" in text and "Go (b.go):" in text
    assert "Java (" not in text and "SQL (" not in text
    assert text.index("Python (") < text.index("Go (")  # table order, not input order
    assert text == language_hints(["a.py", "c.md", "b.go"])


def test_files_of_one_language_are_grouped():
    text = language_hints(["web/b.tsx", "web/a.js"])
    assert text.count("JavaScript/TypeScript (") == 1
    assert "JavaScript/TypeScript (web/a.js, web/b.tsx):" in text


def test_no_known_language_means_no_hints():
    assert language_hints(["README.md", "Dockerfile"]) == ""
    assert language_hints([]) == ""
    assert reviewer_system("", "") == reviewer_system()


def test_hints_are_not_presented_as_team_rules():
    system = reviewer_system("No raw SQL.", language_hints(["a.py"]))
    assert system.index(LANGUAGE_HINTS_HEADER) < system.index("Team conventions")
    assert "No raw SQL." in system


DIFF = """\
diff --git a/app/jobs.py b/app/jobs.py
--- a/app/jobs.py
+++ b/app/jobs.py
@@ -1,1 +1,2 @@
 def run(items=[]):
+    return items
diff --git a/web/api.ts b/web/api.ts
--- a/web/api.ts
+++ b/web/api.ts
@@ -1,1 +1,2 @@
 export function load() {
+  fetch("/x")
"""
FILES = {"app/jobs.py": "def run(items=[]):\n    return items\n", "web/api.ts": 'export function load() {\n  fetch("/x")\n'}


async def _pr(settings) -> PRContext:
    ctx = await build_context(InMemorySource(FILES), parse_unified_diff(DIFF), settings)
    return PRContext(repo="t/t", title="t", files=ctx)


def _specialist_systems(llm: FakeLLM) -> list[str]:
    return [s for name, s in zip(llm.calls, llm.systems) if name == "report_findings"]


async def test_pipeline_adds_hints_identically_to_every_specialist(settings):
    llm = FakeLLM({})
    await review(await _pr(settings), llm, settings)
    systems = _specialist_systems(llm)
    assert len(systems) == len(SPECIALISTS)
    assert len(set(systems)) == 1  # still one cacheable prefix per batch
    system = systems[0]
    assert "Python (app/jobs.py):" in system and "JavaScript/TypeScript (web/api.ts):" in system
    assert "Go (" not in system


async def test_hints_are_scoped_to_each_batch(settings):
    # a tiny prompt budget forces one file per batch
    s = settings.model_copy(update={"max_prompt_chars": 10})
    llm = FakeLLM({})
    await review(await _pr(s), llm, s)
    systems = set(_specialist_systems(llm))
    assert len(systems) == 2
    for system in systems:
        assert ("Python (" in system) != ("JavaScript/TypeScript (" in system)


async def test_language_hints_can_be_turned_off(settings):
    s = settings.model_copy(update={"language_hints": False})
    llm = FakeLLM({})
    await review(await _pr(s), llm, s)
    assert all(LANGUAGE_HINTS_HEADER not in sys for sys in _specialist_systems(llm))
