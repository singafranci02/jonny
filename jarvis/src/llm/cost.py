"""Per-call cost from the pricing table in config.yaml (USD per 1M tokens)."""

from __future__ import annotations

from .base import LLMResponse


def compute_cost(resp: LLMResponse, pricing: dict) -> float | None:
    p = pricing.get(resp.model)
    if p is None:
        return None
    per = 1_000_000
    return (
        resp.input_tokens * p.get("input", 0)
        + resp.output_tokens * p.get("output", 0)
        + resp.cache_read_tokens * p.get("cache_read", 0)
        + resp.cache_write_tokens * p.get("cache_write", 0)
    ) / per
