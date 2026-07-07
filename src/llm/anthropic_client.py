"""Anthropic backend: Claude Sonnet 5 for hard/research tiers.

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

from .base import LLMClient, LLMResponse, ToolCall
from .cost import compute_cost


class AnthropicLLM(LLMClient):
    def __init__(self, cfg: dict):
        self.pricing = cfg["llm"].get("pricing", {})
        self.client = AsyncAnthropic()  # ANTHROPIC_API_KEY from env/.env

    async def chat(
        self,
        system: str,
        messages: list[dict],
        model_cfg: dict,
        tier: str = "default",
        tools: list | None = None,
        on_text=None,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": model_cfg["model"],
            "max_tokens": model_cfg.get("max_tokens", 512),
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }
        thinking = str(model_cfg.get("thinking", "off"))
        if thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
            if model_cfg.get("effort"):
                kwargs["output_config"] = {"effort": model_cfg["effort"]}
        else:
            # Sonnet 5 runs adaptive thinking when the field is omitted;
            # voice turns want the explicit fast path.
            kwargs["thinking"] = {"type": "disabled"}
        if tools:
            kwargs["tools"] = [t.anthropic_schema() for t in tools]

        if on_text is not None:
            async with self.client.messages.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    on_text(delta)
                response = await stream.get_final_message()
        else:
            response = await self.client.messages.create(**kwargs)

        text = "".join(b.text for b in response.content if b.type == "text")
        if response.stop_reason == "refusal":
            text = text or "I can't help with that one."
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]

        result = LLMResponse(
            text=text.strip(),
            model=model_cfg["model"],
            tier=tier,
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
            extra={"raw_content": response.content},
        )
        result.cost_usd = compute_cost(result, self.pricing)
        return result

    def tool_result_messages(self, response, results):
        # assistant turn echoes the raw content (tool_use blocks included);
        # ALL tool_results go back in ONE user message
        return [
            {"role": "assistant", "content": response.extra["raw_content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": output,
                        **({"is_error": True} if is_error else {}),
                    }
                    for call, output, is_error in results
                ],
            },
        ]
