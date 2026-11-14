"""On-disk response cache around any `LLM`.

`CachingLLM` keys each call by a SHA-256 of (model, system, user, tool schema) and stores the tool output as
JSON under `cache_dir`. A rerun with identical prompts — an eval rerun, a re-review of the same head — is served
from disk: no tokens, no latency, and the exact same findings, so results are reproducible.

* Only successful outputs are stored. An error (after the inner `LLM`'s retries) propagates and is never
  cached, so the next run tries again.
* `cache_control` markers are stripped from the key: they change how the provider bills the prompt, not what
  it says, so switching prompt caching on or off doesn't bust the response cache.
* A hit is recorded in usage as a zero-token call with `response_cached=True` (see `usage.summarize`).
* Unreadable or corrupt entries count as misses and are overwritten. Writes are atomic (temp file + rename),
  so concurrent reviews sharing a directory never see half a file.

Wrap it *outside* `ResilientLLM` so hits skip the concurrency limit and the retry loop entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from reviewer.agents.llm import LLM, UserContent
from reviewer.usage import CallUsage, record_call

log = logging.getLogger(__name__)

CACHE_FORMAT = 1  # bump to invalidate every entry if the stored shape or key recipe changes


def _strip_cache_control(user: UserContent) -> UserContent:
    if isinstance(user, str):
        return user
    return [{k: v for k, v in block.items() if k != "cache_control"} for block in user]


def cache_key(*, model: str, system: str, user: UserContent, tool: dict) -> str:
    """Stable hex digest of everything that determines the model's answer."""
    payload = {
        "v": CACHE_FORMAT,
        "model": model,
        "system": system,
        "user": _strip_cache_control(user),
        "tool": tool,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CachingLLM:
    """An `LLM` that answers repeated calls from `cache_dir` and records hits in usage."""

    def __init__(self, inner: LLM, cache_dir: str | os.PathLike[str]):
        self.inner = inner
        self.cache_dir = Path(cache_dir).expanduser()
        self.hits = 0
        self.misses = 0

    def path_for(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("response cache: ignoring unreadable entry %s (%s)", path, e)
            return None
        if not isinstance(entry, dict) or entry.get("v") != CACHE_FORMAT:
            return None
        output = entry.get("output")
        return output if isinstance(output, dict) else None

    def _store(self, path: Path, *, model: str, tool_name: str, output: dict[str, Any]) -> None:
        entry = {"v": CACHE_FORMAT, "model": model, "tool": tool_name, "output": output}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(entry, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError) as e:  # a cache that can't write must not fail the review
            log.warning("response cache: could not write %s (%s)", path, e)

    async def call_tool(
        self, *, model: str, system: str, user: UserContent, tool: dict, max_tokens: int = 8000
    ) -> dict[str, Any]:
        key = cache_key(model=model, system=system, user=user, tool=tool)
        path = self.path_for(key)
        cached = self._load(path)
        if cached is not None:
            self.hits += 1
            record_call(CallUsage(model=model, response_cached=True))
            return cached
        self.misses += 1
        output = await self.inner.call_tool(model=model, system=system, user=user, tool=tool, max_tokens=max_tokens)
        if isinstance(output, dict):
            self._store(path, model=model, tool_name=str(tool.get("name", "")), output=output)
        return output
