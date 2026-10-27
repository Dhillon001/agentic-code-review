"""LLM boundary. Everything goes through `call_tool` (forced tool use = reliable JSON out).

Swap providers or inject a fake in tests by implementing the `LLM` protocol.

`user` is either a plain string or a list of Anthropic-style text content blocks. Callers that fan
the same context out to several agents send it as a leading block marked with `cache_control`
(see `cached_text`) so the provider can reuse the prefix; implementations that don't cache can
flatten the blocks with `as_text`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, Union

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
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
            )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise RuntimeError(f"{model} did not call {tool['name']}")
