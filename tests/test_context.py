from reviewer.context.retriever import build_context, changed_symbols
from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.sources import InMemorySource

DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 def helper_func(x):
-    return x
+    return x + 1
"""


def test_body_only_change_still_finds_enclosing_symbol():
    assert changed_symbols(parse_unified_diff(DIFF)[0]) == ["helper_func"]


async def test_related_callers_pulled_in(settings):
    src = InMemorySource({
        "a.py": "def helper_func(x):\n    return x + 1\n",
        "b.py": "from a import helper_func\nprint(helper_func(1))\n",
        "c.py": "unrelated = True\n",
    })
    [fc] = await build_context(src, parse_unified_diff(DIFF), settings)
    assert [r.path for r in fc.related] == ["b.py"]
    assert "helper_func(1)" in fc.related[0].text
