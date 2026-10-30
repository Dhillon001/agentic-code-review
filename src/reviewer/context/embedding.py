"""Embedding retrieval: find related code by meaning, not just by shared symbol names.

The repo is split into fixed-size line chunks, every chunk is embedded once per review, and each changed
file's hunks are embedded as a query; the top-k chunks by cosine similarity (at most one per file) become
`RelatedSnippet`s. The embedder is injected — any `embed(texts) -> list[list[float]]`, sync or async — so you
can plug in a hosted embedding model or a local one without touching the agents. `hashing_embed` is a
dependency-free default (hashed bag of identifiers) so `retriever: embedding` works out of the box.

Which files get indexed: a source with `list_files()` (local git, in-memory) is indexed whole, up to
`embedding_max_files`; other sources (GitHub) fall back to the files code search returns for the changed
symbols, re-ranked by similarity.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
from collections.abc import Awaitable, Callable, Sequence

from pydantic import BaseModel

from reviewer.config import Settings
from reviewer.context.retriever import changed_symbols, number_lines
from reviewer.ingest.noise import NOISE_PATTERNS
from reviewer.ingest.sources import Source
from reviewer.models import FileDiff, RelatedSnippet
from reviewer.repo_config import path_ignored

Embedder = Callable[[list[str]], "list[list[float]] | Awaitable[list[list[float]]]"]

EMBED_BATCH = 64  # texts per embed() call — keeps hosted APIs happy
MAX_INDEX_FILE_CHARS = 200_000  # skip huge (likely generated) files
MIN_SIMILARITY = 0.05  # below this a "related" chunk is noise
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


class Chunk(BaseModel):
    path: str
    start_line: int  # 1-based
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def chunk_file(path: str, text: str, size: int) -> list[Chunk]:
    lines = text.splitlines()
    return [
        Chunk(path=path, start_line=i + 1, lines=lines[i : i + size])
        for i in range(0, len(lines), size)
        if any(ln.strip() for ln in lines[i : i + size])
    ]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _subtokens(word: str) -> list[str]:
    """`getUserName` / `get_user_name` -> get, user, name (plus the whole word)."""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", word)
    return [word.lower(), *(p.lower() for p in parts if len(p) > 1)]


def hashing_embed(texts: list[str], dim: int = 256) -> list[list[float]]:
    """Deterministic, offline embedder: L2-normalised hashed counts of identifiers and their sub-words.
    Lexical at heart, but it scores partial overlap (shared vocabulary) rather than exact symbol hits."""
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for word in TOKEN_RE.findall(text):
            for tok in _subtokens(word):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / norm for x in vec])
    return out


def query_text(diff: FileDiff) -> str:
    """What the change is about: the post-change hunk lines (added + context), plus the path."""
    body = [ln[1:] for h in diff.hunks for ln in h.lines if not ln.startswith("-")]
    return "\n".join([diff.path, *body])


class EmbeddingRetriever:
    """Holds one index per review: build the retriever per `build_context` call, not globally."""

    def __init__(self, embed: Embedder = hashing_embed, chunk_lines: int = 40, max_files: int = 500):
        self.embed = embed
        self.chunk_lines = chunk_lines
        self.max_files = max_files
        self._chunks: list[Chunk] = []
        self._vectors: list[list[float]] = []
        self._indexed: set[str] = set()
        self._lock = asyncio.Lock()

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            res = self.embed(batch)
            if inspect.isawaitable(res):
                res = await res
            res = list(res)
            if len(res) != len(batch):
                raise ValueError(f"embedder returned {len(res)} vectors for {len(batch)} texts")
            vectors.extend(res)
        return vectors

    async def _candidate_paths(self, source: Source, diff: FileDiff, settings: Settings) -> list[str]:
        list_files = getattr(source, "list_files", None)
        if list_files is not None:
            paths = await list_files()
        else:
            paths = []
            for sym in changed_symbols(diff)[:5]:
                paths.extend(await source.search(sym, limit=10))
        patterns = [*NOISE_PATTERNS, *settings.ignore]
        return [p for p in dict.fromkeys(paths) if not path_ignored(p, patterns)]

    async def _index(self, paths: list[str], source: Source) -> None:
        async with self._lock:
            new = [p for p in paths if p not in self._indexed][: max(0, self.max_files - len(self._indexed))]
            self._indexed.update(new)
            texts = await asyncio.gather(*(source.read_file(p) for p in new))
            chunks = [
                c
                for p, t in zip(new, texts)
                if t and len(t) <= MAX_INDEX_FILE_CHARS
                for c in chunk_file(p, t, self.chunk_lines)
            ]
            if chunks:
                self._vectors.extend(await self._embed([f"{c.path}\n{c.text}" for c in chunks]))
                self._chunks.extend(chunks)

    async def related(self, source: Source, diff: FileDiff, settings: Settings) -> list[RelatedSnippet]:
        await self._index(await self._candidate_paths(source, diff, settings), source)
        if not self._chunks:
            return []
        [q] = await self._embed([query_text(diff)])
        scored = sorted(
            ((cosine(q, v), c) for c, v in zip(self._chunks, self._vectors) if c.path != diff.path),
            key=lambda sc: (-sc[0], sc[1].path, sc[1].start_line),
        )
        out: list[RelatedSnippet] = []
        seen: set[str] = set()
        for score, c in scored:
            if score < MIN_SIMILARITY or len(out) >= settings.max_related_per_file:
                break
            if c.path in seen:
                continue
            seen.add(c.path)
            out.append(
                RelatedSnippet(
                    path=c.path,
                    reason=f"semantically similar (cosine {score:.2f})",
                    start_line=c.start_line,
                    text=number_lines(c.lines, c.start_line),
                )
            )
        return out
