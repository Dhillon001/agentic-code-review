"""Where code comes from. Every source answers two questions: read a file, find files mentioning a symbol."""

from __future__ import annotations

import asyncio
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
