"""OpenAI-compatible escape hatch: DeepSeek, Qwen cloud, Kimi, GLM...

Point a tier at provider: openai_compatible in config.yaml to use it.
Text chat only (no tool calling) — the anthropic/ollama backends are the
first-class citizens.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

from .base import LLMClient, LLMResponse
from .cost import compute_cost


class OpenAICompatibleLLM(LLMClient):
    def __init__(self, cfg: dict):
        ocfg = cfg["llm"]["openai_compatible"]
        self.pricing = ocfg.get("pricing", {})
        # local endpoints (Ollama) ignore the key, but the SDK requires one
        api_key = os.environ.get(ocfg.get("api_key_env", "LLM_API_KEY")) or "not-needed"
        self.client = AsyncOpenAI(base_url=ocfg["base_url"], api_key=api_key)

    async def chat(
        self,
        system: str,
        messages: list[dict],
        model_cfg: dict,
        tier: str = "default",
        tools: list | None = None,
        on_text=None,
    ) -> LLMResponse:
        if tools:
            raise NotImplementedError(
                "tool calling is not implemented for openai_compatible"
            )
        response = await self.client.chat.completions.create(
            model=model_cfg["model"],
            max_tokens=model_cfg.get("max_tokens", 512),
            temperature=model_cfg.get("temperature", 0.6),
            # System prompt first and byte-identical across calls, so
            # providers with automatic prefix caching (DeepSeek etc.) hit it.
            messages=[{"role": "system", "content": system}, *messages],
        )
        usage = response.usage
        cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0

        result = LLMResponse(
            text=(response.choices[0].message.content or "").strip(),
            model=model_cfg["model"],
            tier=tier,
            stop_reason=response.choices[0].finish_reason,
            input_tokens=(usage.prompt_tokens or 0) - cached,
            output_tokens=usage.completion_tokens or 0,
            cache_read_tokens=cached,
        )
        result.cost_usd = compute_cost(result, self.pricing)
        return result

    def tool_result_messages(self, response, results):
        raise NotImplementedError
