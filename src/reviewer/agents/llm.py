"""LLM boundary. Everything goes through `call_tool` (forced tool use = reliable JSON out).

Swap providers or inject a fake in tests by implementing the `LLM` protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol


class LLM(Protocol):
    async def call_tool(
        self, *, model: str, system: str, user: str, tool: dict, max_tokens: int = 8000
    ) -> dict[str, Any]: ...


class AnthropicLLM:
    def __init__(self, api_key: str, concurrency: int = 4):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, max_retries=3)
        self._sem = asyncio.Semaphore(concurrency)

    async def call_tool(
        self, *, model: str, system: str, user: str, tool: dict, max_tokens: int = 8000
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
