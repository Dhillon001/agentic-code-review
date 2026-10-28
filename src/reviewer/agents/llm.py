"""LLM boundary. Everything goes through `call_tool` (forced tool use = reliable JSON out).

Swap providers or inject a fake in tests by implementing the `LLM` protocol.

`user` is either a plain string or a list of Anthropic-style text content blocks. Callers that fan
the same context out to several agents send it as a leading block marked with `cache_control`
(see `cached_text`) so the provider can reuse the prefix; implementations that don't cache can
flatten the blocks with `as_text`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol, Union

from reviewer.usage import CallUsage, record_call

ContentBlock = dict[str, Any]
UserContent = Union[str, list[ContentBlock]]

EPHEMERAL = {"type": "ephemeral"}


def text_block(text: str) -> ContentBlock:
    return {"type": "text", "text": text}


def cached_text(text: str) -> ContentBlock:
    """A text block marked as a prompt-cache breakpoint: everything up to and including it is cacheable."""
    return {"type": "text", "text": text, "cache_control": dict(EPHEMERAL)}


def as_text(user: UserContent) -> str:
    """Flatten `user` content to one string (for fakes, logging, and size estimates)."""
    if isinstance(user, str):
        return user
    return "\n\n".join(b.get("text", "") for b in user if b.get("type") == "text")


class LLM(Protocol):
    async def call_tool(
        self, *, model: str, system: str, user: UserContent, tool: dict, max_tokens: int = 8000
    ) -> dict[str, Any]: ...


class AnthropicLLM:
    def __init__(self, api_key: str, concurrency: int = 4):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, max_retries=3)
        self._sem = asyncio.Semaphore(concurrency)

    async def call_tool(
        self, *, model: str, system: str, user: UserContent, tool: dict, max_tokens: int = 8000
    ) -> dict[str, Any]:
        async with self._sem:
            t0 = time.perf_counter()
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
            )
            latency = time.perf_counter() - t0  # excludes time queued on the semaphore
        record_call(usage_from_response(model, resp, latency))
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise RuntimeError(f"{model} did not call {tool['name']}")


def usage_from_response(model: str, resp: Any, latency_s: float) -> CallUsage:
    """Read token counts off an Anthropic response; missing fields (older SDKs, fakes) count as 0."""
    u = getattr(resp, "usage", None)

    def n(field: str) -> int:
        return int(getattr(u, field, 0) or 0)

    return CallUsage(
        model=model,
        input_tokens=n("input_tokens"),
        output_tokens=n("output_tokens"),
        cache_read_tokens=n("cache_read_input_tokens"),
        cache_write_tokens=n("cache_creation_input_tokens"),
        latency_s=latency_s,
    )
