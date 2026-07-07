from .base import LLMClient, LLMResponse, ToolCall


class TieredLLM:
    """Routes each tier (default/hard/research/summarize) to its configured
    provider + model. Backends are constructed lazily, one per provider.

    If a cloud tier fails (API down, no key), the turn degrades to the
    `default` tier when that tier is local.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tiers: dict = cfg["llm"]["tiers"]
        self._backends: dict[str, LLMClient] = {}

    def _backend(self, provider: str) -> LLMClient:
        if provider not in self._backends:
            if provider == "anthropic":
                from .anthropic_client import AnthropicLLM

                self._backends[provider] = AnthropicLLM(self.cfg)
            elif provider == "ollama":
                from .ollama_client import OllamaLLM

                self._backends[provider] = OllamaLLM(self.cfg)
            elif provider == "openai_compatible":
                from .openai_compat import OpenAICompatibleLLM

                self._backends[provider] = OpenAICompatibleLLM(self.cfg)
            else:
                raise ValueError(f"Unknown provider: {provider!r}")
        return self._backends[provider]

    def backend_for(self, tier: str) -> tuple[LLMClient, dict]:
        model_cfg = self.tiers[tier]
        return self._backend(model_cfg["provider"]), model_cfg

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tier: str = "default",
        tools: list | None = None,
        on_text=None,
    ) -> LLMResponse:
        backend, model_cfg = self.backend_for(tier)
        try:
            resp = await backend.chat(system, messages, model_cfg, tier, tools, on_text)
        except Exception:
            fallback = self.tiers.get("default", {})
            if model_cfg["provider"] == "ollama" or fallback.get("provider") != "ollama":
                raise
            backend, model_cfg = self.backend_for("default")
            resp = await backend.chat(system, messages, model_cfg, tier, tools, on_text)
            resp.extra["degraded"] = True
        resp.extra["provider"] = model_cfg["provider"]
        return resp

    def tool_messages(self, response: LLMResponse, results) -> list[dict]:
        """Provider-format messages continuing a tool-call exchange."""
        return self._backends[response.extra["provider"]].tool_result_messages(
            response, results
        )


def make_llm_client(cfg: dict) -> TieredLLM:
    return TieredLLM(cfg)
