"""Cost budget (`MAX_COST_USD`): estimate what the specialist fan-out will cost *before* calling any model,
and drop the lowest-priority files until the estimate fits.

The estimate covers specialist input tokens, which dominate a review's bill: every specialist reads the whole
batch. Tokens are approximated as characters / 4. Prompt caching is priced the way the pipeline uses it: the
shared prefix (system prompt + PR context) is written to the cache by the first call per model and read by the
others when `cache_warmup` is on (each cold parallel call writes it otherwise); the short per-specialist
instructions are plain input. Output tokens and verifier calls depend on what the agents find and are not
estimated, so leave some headroom. Models with no entry in `MODEL_PRICES` count as free here (the usage footer
still flags their calls as unpriced).

Priority is the order of the file list: the last file goes first. The pipeline sorts by risk first
(`reviewer.risk.by_risk`), so the lowest-risk files are the ones dropped.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence

from reviewer.agents.specialists import Specialist, batch_system, build_review_prompt
from reviewer.config import Settings
from reviewer.models import FileContext, PRContext
from reviewer.usage import CallUsage, price_for

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)  # ceil: an empty prompt is 0, any text is at least 1


def estimate_cost(
    pr: PRContext, files: list[FileContext], specialists: Sequence[Specialist], settings: Settings
) -> float:
    """Estimated USD for the specialist input tokens of reviewing `files` (batched as the pipeline does)."""
    from reviewer.pipeline import batch_files  # pipeline imports this module

    if not files or not specialists:
        return 0.0
    per_model = Counter(settings.model_for(s.name) for s in specialists)
    total = 0.0
    for batch in batch_files(files, settings):
        prefix = estimate_tokens(batch_system(batch, settings)) + estimate_tokens(
            build_review_prompt(pr, batch, settings)
        )
        for spec in specialists:
            total += _price(settings.model_for(spec.name), settings, input_tokens=estimate_tokens(spec.instructions))
        for model, n in per_model.items():
            writes = 1 if settings.cache_warmup else n
            total += _price(model, settings, cache_write_tokens=prefix * writes, cache_read_tokens=prefix * (n - writes))
    return total


def _price(model: str, settings: Settings, **tokens: int) -> float:
    price = price_for(model, settings.model_prices)
    return price.cost(CallUsage(model=model, **tokens)) if price else 0.0


def fit_budget(
    pr: PRContext, files: list[FileContext], specialists: Sequence[Specialist], settings: Settings
) -> tuple[list[FileContext], list[FileContext], float]:
    """`(kept, dropped, estimate)`: drop files from the end of `files` until the estimated cost of reviewing the
    rest is within `settings.max_cost_usd` (no budget, or a non-positive one, keeps everything). `dropped` is in
    the order files were dropped; `estimate` is the estimated cost of the kept files."""
    budget = settings.max_cost_usd
    kept = list(files)
    estimate = estimate_cost(pr, kept, specialists, settings)
    if budget is None or budget <= 0:
        return kept, [], estimate
    dropped: list[FileContext] = []
    while kept and estimate > budget:
        dropped.append(kept.pop())
        estimate = estimate_cost(pr, kept, specialists, settings)
    if dropped:
        log.warning(
            "cost budget $%.2f: skipping %d file(s) (%s); estimated cost of the rest $%.4f",
            budget, len(dropped), ", ".join(fc.path for fc in dropped), estimate,
        )
    return kept, dropped, estimate
