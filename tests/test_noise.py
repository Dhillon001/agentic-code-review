from __future__ import annotations

import subprocess

import pytest
from conftest import FakeLLM

from reviewer.ingest.diff import parse_unified_diff
from reviewer.ingest.noise import NOISE_PATTERNS, is_noise, split_noise
from reviewer.models import FileDiff, PRContext
from reviewer.pipeline import review
from reviewer.repo_config import apply_repo_config, parse_repo_config
from reviewer.service import context_from_github, context_from_local

DIFF = """\
diff --git a/app/x.py b/app/x.py
--- a/app/x.py
+++ b/app/x.py
@@ -1,1 +1,2 @@
 a = 1
+b = 2
diff --git a/poetry.lock b/poetry.lock
--- a/poetry.lock
+++ b/poetry.lock
@@ -1,1 +1,2 @@
 [[package]]
+name = "x"
diff --git a/web/package-lock.json b/web/package-lock.json
--- a/web/package-lock.json
+++ b/web/package-lock.json
@@ -1,1 +1,2 @@
 {
+  "x": 1
diff --git a/static/app.min.js b/static/app.min.js
--- a/static/app.min.js
+++ b/static/app.min.js
@@ -1 +1 @@
-var a=1;
+var a=2;
diff --git a/img/logo.png b/img/logo.png
new file mode 100644
index 0000000..1111111
Binary files /dev/null and b/img/logo.png differ
diff --git a/docs/guide.md b/docs/guide.md
--- a/docs/guide.md
+++ b/docs/guide.md
@@ -1,1 +1,2 @@
 # Guide
+more
"""


@pytest.mark.parametrize(
    "path",
    [
        "poetry.lock", "uv.lock", "sub/Cargo.lock", "yarn.lock", "package-lock.json", "web/pnpm-lock.yaml",
        "go.sum", "static/app.min.js", "css/site.min.css", "static/app.js.map", "proto/user_pb2.py",
        "proto/user_pb2_grpc.py", "api/user.pb.go", "src/schema.generated.ts", "dist/bundle.js",
        "pkg/dist/index.js", "vendor/github.com/x/y.go", "web/node_modules/left-pad/index.js",
    ],
)
def test_default_noise(path):
    assert is_noise(FileDiff(path=path))


@pytest.mark.parametrize(
    "path", ["app/x.py", "lock.py", "src/clock.py", "distance.py", "app/vendors.py", "app.js", "go.mod"]
)
def test_real_code_kept(path):
    assert not is_noise(FileDiff(path=path))


def test_binary_diff_parsed_and_skipped():
    files = {f.path: f for f in parse_unified_diff(DIFF)}
    logo = files["img/logo.png"]
    assert logo.binary and logo.status == "added" and not logo.hunks
    assert not files["app/x.py"].binary
    assert is_noise(logo)


def test_git_binary_patch_marks_binary():
    diff = (
        "diff --git a/blob.bin b/blob.bin\nindex 1..2 100644\nGIT binary patch\nliteral 3\n"
        "KcmZ?wE&u=k0RR91\n\nliteral 0\nHcmV?d00001\n"
    )
    [f] = parse_unified_diff(diff)
    assert f.binary and not f.hunks


def test_split_noise_and_extra_ignore_extends_defaults():
    diffs = parse_unified_diff(DIFF)
    kept, skipped = split_noise(diffs)
    assert [d.path for d in kept] == ["app/x.py", "docs/guide.md"]
    assert skipped == ["poetry.lock", "web/package-lock.json", "static/app.min.js", "img/logo.png"]

    kept, skipped = split_noise(diffs, ["docs/"])
    assert [d.path for d in kept] == ["app/x.py"]
    assert len(skipped) == 5


def test_defaults_are_one_list_and_not_in_settings(settings):
    # .acr.yml extends the defaults; it doesn't need to repeat them.
    assert "*.lock" in NOISE_PATTERNS
    s = apply_repo_config(settings, parse_repo_config("ignore: [docs/]\n"))
    assert s.ignore == ["docs/"]


# ---------------------------------------------------------------- service + pipeline wiring


class FakeGitHub:
    async def get_pr(self, repo, number):
        return {"title": "t", "body": "", "head": {"sha": "head1", "repo": {"full_name": repo}},
                "base": {"sha": "base1"}}

    async def get_pr_diff(self, repo, number):
        return DIFF

    async def get_file(self, repo, path, ref):
        if ref == "base1" and path == ".acr.yml":
            return "ignore: [docs/]\n"
        assert path in ("app/x.py",), f"noise file {path} should never be read"
        return "a = 1\nb = 2\n"

    async def search_code(self, query, repo, limit=5):
        return []


async def test_github_skips_noise_before_context_and_reports_count(settings):
    pr, merged = await context_from_github(FakeGitHub(), "o/r", 1, settings)
    assert [fc.path for fc in pr.files] == ["app/x.py"]
    assert len(pr.skipped) == 5

    result = await review(pr, FakeLLM({}), merged)
    assert result.stats["skipped"] == 5 and result.stats["files"] == 1


async def test_all_noise_still_reports_skipped(settings):
    result = await review(PRContext(repo="t/t", skipped=["poetry.lock"]), FakeLLM({}), settings)
    assert result.stats == {"files": 0, "skipped": 1}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def test_local_skips_lockfile_and_binary(tmp_path, settings):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("a = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    (tmp_path / "app.py").write_text("a = 1\nb = 2\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "change")

    pr, _ = await context_from_local(tmp_path, "main", "HEAD", settings)
    assert [fc.path for fc in pr.files] == ["app.py"]
    assert sorted(pr.skipped) == ["logo.png", "uv.lock"]
