"""Anthropic backend (default): Claude Sonnet 5 via the official SDK.

Caching notes:
- The system prompt carries a cache_control breakpoint, so keep it
  byte-identical across calls (prompts/system.md is loaded once).
- Volatile per-turn context (memories, knowledge chunks) belongs in the
  user message, after the cached prefix — never in the system prompt.
Sonnet 5 rejects non-default sampling params (temperature/top_p/top_k),
so tone is steered by the prompt instead.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic

from .base import LLMClient, LLMResponse
from .cost import compute_cost


class AnthropicLLM(LLMClient):
    def __init__(self, cfg: dict):
        acfg = cfg["llm"]["anthropic"]
        self.models = acfg["models"]
        self.pricing = acfg.get("pricing", {})
        self.client = AsyncAnthropic()  # ANTHROPIC_API_KEY from env/.env

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tier: str = "default",
    ) -> LLMResponse:
        m = self.models[tier]
        kwargs: dict = {
            "model": m["id"],
            "max_tokens": m.get("max_tokens", 512),
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }
        thinking = str(m.get("thinking", "off"))
        if thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
            if m.get("effort"):
                kwargs["output_config"] = {"effort": m["effort"]}
        else:
            # Sonnet 5 runs adaptive thinking when the field is omitted;
            # voice turns want the explicit fast path.
            kwargs["thinking"] = {"type": "disabled"}

        response = await self.client.messages.create(**kwargs)

        text = "".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "refusal":
            text = text or "I can't help with that one."

        result = LLMResponse(
            text=text.strip(),
            model=m["id"],
            tier=tier,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        result.cost_usd = compute_cost(result, self.pricing)
        return result
