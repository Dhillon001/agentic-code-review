"""Where code comes from. Every source answers two questions: read a file, find files mentioning a symbol."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from reviewer.ingest.github import GitHubClient


class Source(Protocol):
    async def read_file(self, path: str) -> str | None: ...

    async def search(self, symbol: str, limit: int = 5) -> list[str]: ...


# Optional extra a source may implement: `async def list_files(self) -> list[str]` (every file at the
# reviewed ref). The embedding retriever indexes those; without it, it falls back to search results.


class GitHubSource:
    def __init__(self, client: GitHubClient, repo: str, ref: str):
        self.client, self.repo, self.ref = client, repo, ref
        self._cache: dict[str, str | None] = {}

    async def read_file(self, path: str) -> str | None:
        if path not in self._cache:
            self._cache[path] = await self.client.get_file(self.repo, path, self.ref)
        return self._cache[path]

    async def search(self, symbol: str, limit: int = 5) -> list[str]:
        return await self.client.search_code(symbol, self.repo, limit)


class LocalGitSource:
    """Review a local branch against a base — no GitHub needed. Great for dev and evals."""

    def __init__(self, repo: str | Path, base: str = "main", head: str = "HEAD"):
        self.repo, self.base, self.head = Path(repo).resolve(), base, head

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=False
        ).stdout

    def diff(self) -> str:
        return self._git("diff", "--no-color", "-U3", f"{self.base}...{self.head}")

    async def read_file(self, path: str) -> str | None:
        out = await asyncio.to_thread(self._git, "show", f"{self.head}:{path}")
        return out or None

    async def search(self, symbol: str, limit: int = 5) -> list[str]:
        out = await asyncio.to_thread(self._git, "grep", "-l", "-w", "-F", "-e", symbol, self.head)
        prefix = f"{self.head}:"
        paths = [p.removeprefix(prefix) for p in out.splitlines() if p]
        return paths[:limit]

    async def list_files(self) -> list[str]:
        out = await asyncio.to_thread(self._git, "ls-tree", "-r", "--name-only", self.head)
        return [p for p in out.splitlines() if p]


class StagedGitSource:
    """What `git commit` would record: the staged diff (`git diff --cached`) with file contents read from
    the index, not the working tree, so unstaged edits never leak into the review. Used by `acr staged`
    and the pre-commit hook. Works before the first commit (the diff is then against the empty tree)."""

    def __init__(self, repo: str | Path = "."):
        top = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
        )
        if top.returncode != 0:
            raise ValueError(f"not a git repository: {repo} ({top.stderr.strip()})")
        # Diff paths are relative to the top level, and so are index reads, wherever --repo points.
        self.repo = Path(top.stdout.strip()).resolve()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), "-c", "core.quotepath=off", *args],
            capture_output=True, text=True, check=False,
        )

    def diff(self) -> str:
        out = self._run("diff", "--cached", "--no-color", "--no-ext-diff", "-U3")
        if out.returncode != 0:
            raise RuntimeError(f"git diff --cached failed: {out.stderr.strip()}")
        return out.stdout

    async def read_file(self, path: str) -> str | None:
        out = await asyncio.to_thread(self._run, "show", f":{path}")
        return out.stdout if out.returncode == 0 and out.stdout else None

    async def search(self, symbol: str, limit: int = 5) -> list[str]:
        out = await asyncio.to_thread(self._run, "grep", "--cached", "-l", "-w", "-F", "-e", symbol)
        return [p for p in out.stdout.splitlines() if p][:limit]

    async def list_files(self) -> list[str]:
        out = await asyncio.to_thread(self._run, "ls-files", "--cached")
        return [p for p in out.stdout.splitlines() if p]


class FilesystemSource:
    """Read files straight from a directory — no git or GitHub. Used by `acr diff`, where the patch is
    assumed to be applied already (the directory holds the post-change files).

    Paths come from a patch that may be untrusted, so anything resolving outside `root` reads as missing."""

    SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
                           ".mypy_cache", ".pytest_cache", "dist", "build"})
    MAX_BYTES = 1_000_000  # search/list skip bigger files: generated data, not code worth context

    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()

    def _resolve(self, path: str) -> Path | None:
        target = (self.root / path).resolve()
        return target if target.is_relative_to(self.root) and target.is_file() else None

    def _read(self, path: str) -> str | None:
        target = self._resolve(path)
        if target is None:
            return None
        try:
            data = target.read_bytes()
        except OSError:
            return None
        if b"\0" in data[:8192]:  # binary
            return None
        return data.decode("utf-8", errors="replace")

    def _walk(self) -> list[str]:
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                try:
                    if full.is_symlink() or full.stat().st_size > self.MAX_BYTES:
                        continue
                except OSError:
                    continue
                out.append(full.relative_to(self.root).as_posix())
        return out

    def _search(self, symbol: str, limit: int) -> list[str]:
        pat = re.compile(rf"\b{re.escape(symbol)}\b")
        hits: list[str] = []
        for rel in self._walk():
            text = self._read(rel)
            if text is not None and pat.search(text):
                hits.append(rel)
                if len(hits) >= limit:
                    break
        return hits

    async def read_file(self, path: str) -> str | None:
        return await asyncio.to_thread(self._read, path)

    async def search(self, symbol: str, limit: int = 5) -> list[str]:
        return await asyncio.to_thread(self._search, symbol, limit)

    async def list_files(self) -> list[str]:
        return await asyncio.to_thread(self._walk)


class InMemorySource:
    """Dict-backed source for tests and eval cases."""

    def __init__(self, files: dict[str, str]):
        self.files = files

    async def read_file(self, path: str) -> str | None:
        return self.files.get(path)

    async def search(self, symbol: str, limit: int = 5) -> list[str]:
        pat = re.compile(rf"\b{re.escape(symbol)}\b")
        return [p for p, c in self.files.items() if pat.search(c)][:limit]

    async def list_files(self) -> list[str]:
        return list(self.files)
