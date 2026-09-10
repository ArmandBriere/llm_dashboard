"""Model pricing used to estimate API-equivalent cost of Claude Code turns.

Subscriptions are flat-rate, so these dollars are not what you pay. They are a
relative weight that turns raw token counts into something closer to how the
usage window is consumed (a cache read costs a tenth of a fresh input token,
output tokens cost several times more than input, and bigger models cost more
than smaller ones). Keep the table in sync with https://www.anthropic.com/pricing
when it changes; unknown models fall back to the Opus tier.
"""

from __future__ import annotations

# USD per million tokens: (input, output). Cache writes are billed at 1.25x
# input, cache reads at 0.1x input, the same multipliers Anthropic publishes.
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "fable": (15.0, 75.0),
    "mythos": (15.0, 75.0),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
DEFAULT_TIER = "opus"
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def model_tier(model: str | None) -> str:
    """Map a full model id such as ``claude-fable-5-1`` to a pricing tier."""
    name = (model or "").lower()
    for tier in MODEL_PRICING_PER_MTOK:
        if tier in name:
            return tier
    return DEFAULT_TIER


def estimate_cost_usd(
    model: str | None,
    input_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Estimate the API-equivalent cost of one turn in USD."""
    in_price, out_price = MODEL_PRICING_PER_MTOK[model_tier(model)]
    cost = (
        input_tokens * in_price
        + cache_creation_tokens * in_price * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * in_price * CACHE_READ_MULTIPLIER
        + output_tokens * out_price
    )
    return cost / 1_000_000.0


def pretty_model_name(model: str | None) -> str:
    """Turn ``claude-fable-5-1`` into ``Fable 5.1`` for display."""
    if not model:
        return "Unknown"
    if model.startswith("<"):
        return model.strip("<>").capitalize()
    parts = model.split("-")
    if parts and parts[0] == "claude":
        parts = parts[1:]
    if not parts:
        return model
    family = parts[0].capitalize()
    version = ".".join(p for p in parts[1:] if p.isdigit())
    return f"{family} {version}".strip()
