"""Timeouts and retries around any `LLM`.

`ResilientLLM` wraps another `LLM` and gives every call a per-attempt timeout plus retries with exponential
backoff and full jitter on *transient* errors (rate limits, overload, 5xx, timeouts, dropped connections).
Anything else (bad request, auth, a model that didn't call the tool) fails fast: retrying won't fix it.

It also owns the concurrency limit, so the timeout clock only runs while a call is actually in flight
(not while it waits for a slot) and a call backing off releases its slot for others.

A call that still fails raises; the pipeline turns that into a `failed_agents` entry instead of a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from reviewer.agents.llm import LLM, UserContent

log = logging.getLogger(__name__)

# HTTP statuses worth retrying: request timeout, conflict/lock, rate limit, server errors, Anthropic "overloaded".
TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
# Provider exception class names that are transient whatever their status (no SDK import needed).
TRANSIENT_NAMES = frozenset(
    {"RateLimitError", "OverloadedError", "InternalServerError", "APITimeoutError", "APIConnectionError"}
)


class RetryPolicy(BaseModel):
    timeout_s: float | None = Field(default=180.0, gt=0)  # per attempt; None = no timeout
    max_retries: int = Field(default=3, ge=0)  # attempts = max_retries + 1
    backoff_base_s: float = Field(default=1.0, ge=0)
    backoff_max_s: float = Field(default=30.0, ge=0)

    def delay(self, retry: int, rng: Callable[[], float] = random.random) -> float:
        """Full jitter: uniform in [0, min(max, base * 2**retry)] for the `retry`-th retry (0-based)."""
        cap = min(self.backoff_max_s, self.backoff_base_s * (2**retry))
        return cap * rng()


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in TRANSIENT_STATUS:
        return True
    return any(cls.__name__ in TRANSIENT_NAMES for cls in type(exc).__mro__)


def retry_after_s(exc: BaseException) -> float | None:
    """Honour a server's `retry-after` header (seconds) when the error carries a response."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class ResilientLLM:
    """An `LLM` that retries its inner `LLM`. `sleep` and `rng` are injectable so tests never wait."""

    def __init__(
        self,
        inner: LLM,
        policy: RetryPolicy | None = None,
        concurrency: int | None = None,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ):
        self.inner = inner
        self.policy = policy or RetryPolicy()
        self._sem = asyncio.Semaphore(concurrency) if concurrency else None
        self._sleep = sleep
        self._rng = rng

    async def _attempt(self, **kw: Any) -> dict[str, Any]:
        async with self._sem or contextlib.nullcontext():
            if self.policy.timeout_s is None:
                return await self.inner.call_tool(**kw)
            return await asyncio.wait_for(self.inner.call_tool(**kw), self.policy.timeout_s)

    async def call_tool(
        self, *, model: str, system: str, user: UserContent, tool: dict, max_tokens: int = 8000
    ) -> dict[str, Any]:
        kw = {"model": model, "system": system, "user": user, "tool": tool, "max_tokens": max_tokens}
        retry = 0
        while True:
            try:
                return await self._attempt(**kw)
            except Exception as e:  # CancelledError is a BaseException: cancellation is never retried
                if not is_transient(e) or retry >= self.policy.max_retries:
                    raise
                hinted = retry_after_s(e)
                delay = self.policy.delay(retry, self._rng)
                if hinted is not None:
                    delay = min(max(delay, hinted), self.policy.backoff_max_s)  # never wait past the cap
                retry += 1
                log.info(
                    "%s %s call failed (%s); retry %d/%d in %.1fs",
                    model, tool.get("name"), type(e).__name__, retry, self.policy.max_retries, delay,
                )
                await self._sleep(delay)
