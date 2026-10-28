"""Token, cost, and latency accounting.

`AnthropicLLM` reports every call with `record_call`. A review wraps its work in `recording()`, which
collects the calls made inside it (including from tasks spawned with `asyncio.gather`, which inherit
the context) without threading a recorder through every function — and without mixing up two reviews
running concurrently in the webhook server. Outside a `recording()` block, `record_call` is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, Field


class CallUsage(BaseModel):
    """One model call. `input_tokens` excludes cached tokens (that's how the API reports it)."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_s: float = 0.0


class ModelPrice(BaseModel):
    """USD per million tokens. Cache prices default to the usual multiples of the input price."""

    input: float
    output: float
    cache_read: float | None = None  # default 0.1x input
    cache_write: float | None = None  # default 1.25x input (5-minute ephemeral cache)

    def cost(self, call: CallUsage) -> float:
        cache_read = self.cache_read if self.cache_read is not None else self.input * 0.1
        cache_write = self.cache_write if self.cache_write is not None else self.input * 1.25
        return (
            call.input_tokens * self.input
            + call.output_tokens * self.output
            + call.cache_read_tokens * cache_read
            + call.cache_write_tokens * cache_write
        ) / 1_000_000


# Keys match a model name exactly or as a prefix (longest wins), so "claude-opus" prices every Opus.
# Estimates only: override with MODEL_PRICES (JSON) when your rates differ.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus": ModelPrice(input=5.0, output=25.0),
    "claude-sonnet": ModelPrice(input=3.0, output=15.0),
    "claude-haiku": ModelPrice(input=1.0, output=5.0),
}


def price_for(model: str, prices: dict[str, ModelPrice]) -> ModelPrice | None:
    if model in prices:
        return prices[model]
    matches = [k for k in prices if model.startswith(k)]
    return prices[max(matches, key=len)] if matches else None


class Usage(BaseModel):
    """Aggregate for one review. `est_usd` covers priced calls only; see `unpriced_calls`."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    est_usd: float = 0.0
    unpriced_calls: int = 0
    model_time_s: float = 0.0  # sum of per-call latencies (exceeds wall time when calls overlap)
    wall_time_s: float = 0.0
    by_model: dict[str, CallUsage] = Field(default_factory=dict)

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def summarize(calls: list[CallUsage], prices: dict[str, ModelPrice], wall_time_s: float = 0.0) -> Usage:
    u = Usage(calls=len(calls), wall_time_s=round(wall_time_s, 3))
    cost = 0.0
    for c in calls:
        u.input_tokens += c.input_tokens
        u.output_tokens += c.output_tokens
        u.cache_read_tokens += c.cache_read_tokens
        u.cache_write_tokens += c.cache_write_tokens
        u.model_time_s += c.latency_s
        m = u.by_model.setdefault(c.model, CallUsage(model=c.model))
        m.input_tokens += c.input_tokens
        m.output_tokens += c.output_tokens
        m.cache_read_tokens += c.cache_read_tokens
        m.cache_write_tokens += c.cache_write_tokens
        m.latency_s += c.latency_s
        price = price_for(c.model, prices)
        if price is None:
            u.unpriced_calls += 1
        else:
            cost += price.cost(c)
    u.est_usd = round(cost, 6)
    u.model_time_s = round(u.model_time_s, 3)
    return u


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_usage(u: Usage) -> str:
    """One line, e.g. `12 calls · 48.2k in (31.0k cached) · 3.4k out · ~$0.21 · 38.4s`."""
    parts = [f"{u.calls} call{'s' if u.calls != 1 else ''}"]
    tokens_in = f"{_k(u.total_input_tokens)} in"
    if u.cache_read_tokens:
        tokens_in += f" ({_k(u.cache_read_tokens)} cached)"
    parts += [tokens_in, f"{_k(u.output_tokens)} out"]
    cost = f"~${u.est_usd:.2f}"
    if u.unpriced_calls:
        cost += f" (+{u.unpriced_calls} unpriced)"
    parts += [cost, f"{u.wall_time_s:.1f}s"]
    return " · ".join(parts)


_current: ContextVar[list[CallUsage] | None] = ContextVar("acr_usage", default=None)


def record_call(call: CallUsage) -> None:
    calls = _current.get()
    if calls is not None:
        calls.append(call)


@contextmanager
def recording() -> Iterator[list[CallUsage]]:
    """Collect every `record_call` made inside the block (and in tasks it spawns)."""
    calls: list[CallUsage] = []
    token = _current.set(calls)
    try:
        yield calls
    finally:
        _current.reset(token)
