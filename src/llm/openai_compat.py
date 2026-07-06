"""OpenAI-compatible backend: DeepSeek, Qwen, Kimi, GLM, local Ollama...

Selected via config.yaml (llm.provider: openai_compatible). Point
base_url + model ids at any OpenAI-style endpoint; no code changes.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

from .base import LLMClient, LLMResponse
from .cost import compute_cost


class OpenAICompatibleLLM(LLMClient):
    def __init__(self, cfg: dict):
        ocfg = cfg["llm"]["openai_compatible"]
        self.models = ocfg["models"]
        self.pricing = ocfg.get("pricing", {})
        api_key = os.environ.get(ocfg.get("api_key_env", "LLM_API_KEY"), "")
        self.client = AsyncOpenAI(base_url=ocfg["base_url"], api_key=api_key)

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tier: str = "default",
    ) -> LLMResponse:
        m = self.models[tier]
        response = await self.client.chat.completions.create(
            model=m["id"],
            max_tokens=m.get("max_tokens", 512),
            temperature=m.get("temperature", 0.6),
            # System prompt first and byte-identical across calls, so
            # providers with automatic prefix caching (DeepSeek etc.) hit it.
            messages=[{"role": "system", "content": system}, *messages],
        )
        usage = response.usage
        cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0

        result = LLMResponse(
            text=(response.choices[0].message.content or "").strip(),
            model=m["id"],
            tier=tier,
            stop_reason=response.choices[0].finish_reason,
            input_tokens=(usage.prompt_tokens or 0) - cached,
            output_tokens=usage.completion_tokens or 0,
            cache_read_tokens=cached,
        )
        result.cost_usd = compute_cost(result, self.pricing)
        return result
