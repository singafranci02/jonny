"""Local backend: any Ollama model (default tier runs here — free, private).

keep_alive keeps the model resident between turns so voice latency stays low.
"""

from __future__ import annotations

from .base import LLMClient, LLMResponse, ToolCall


class OllamaLLM(LLMClient):
    def __init__(self, cfg: dict):
        import ollama

        ocfg = cfg["llm"].get("ollama", {})
        self.client = ollama.AsyncClient(host=ocfg.get("url", "http://localhost:11434"))
        self.keep_alive = ocfg.get("keep_alive", "30m")

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
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {"num_predict": model_cfg.get("max_tokens", 512)},
            "keep_alive": self.keep_alive,
            "think": bool(model_cfg.get("thinking", False)),
        }
        if tools:
            kwargs["tools"] = [t.ollama_schema() for t in tools]

        if on_text is not None:
            content_parts: list[str] = []
            raw_tool_calls = []
            response = None
            async for chunk in await self.client.chat(stream=True, **kwargs):
                if chunk.message.content:
                    content_parts.append(chunk.message.content)
                    on_text(chunk.message.content)
                if chunk.message.tool_calls:
                    raw_tool_calls.extend(chunk.message.tool_calls)
                response = chunk  # final chunk carries done_reason + counts
            msg = response.message
            msg.content = "".join(content_parts)
            msg.tool_calls = raw_tool_calls or None
        else:
            response = await self.client.chat(**kwargs)
            msg = response.message

        tool_calls = [
            ToolCall(
                id=f"call_{i}",
                name=tc.function.name,
                arguments=dict(tc.function.arguments or {}),
            )
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        return LLMResponse(
            text=(msg.content or "").strip(),
            model=model_cfg["model"],
            tier=tier,
            stop_reason="tool_use" if tool_calls else response.done_reason,
            tool_calls=tool_calls,
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
            cost_usd=0.0,
            extra={"raw_message": msg},
        )

    def tool_result_messages(self, response, results):
        assistant = {
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {
                    "function": {
                        "name": c.name,
                        "arguments": c.arguments,
                    }
                }
                for c, _, _ in results
            ],
        }
        tool_msgs = [
            {
                "role": "tool",
                "tool_name": call.name,
                "content": output if not is_error else f"ERROR: {output}",
            }
            for call, output, is_error in results
        ]
        return [assistant, *tool_msgs]
