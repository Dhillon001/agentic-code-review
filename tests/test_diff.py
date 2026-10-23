from reviewer.ingest.diff import parse_unified_diff

DIFF = """\
diff --git a/app/x.py b/app/x.py
index 111..222 100644
--- a/app/x.py
+++ b/app/x.py
@@ -10,4 +10,5 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
--- weird removed line
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-z = 1
"""


def test_parses_files_and_status():
    files = parse_unified_diff(DIFF)
    assert [(f.path, f.status) for f in files] == [
        ("app/x.py", "modified"),
        ("new.py", "added"),
        ("gone.py", "deleted"),
    ]


def test_line_numbers():
    x = parse_unified_diff(DIFF)[0]
    assert x.added_lines == {11, 12}
    assert x.commentable_lines == {10, 11, 12, 13}


def test_removed_line_starting_with_dashes_stays_in_hunk():
    x = parse_unified_diff(DIFF)[0]
    assert x.hunks[0].lines[-1] == "--- weird removed line"
    assert x.path == "app/x.py"


def test_render_numbered_gutter():
    out = parse_unified_diff(DIFF)[0].render_numbered()
    assert "   11 +     b = 3" in out
    assert "      -     b = 2" in out
