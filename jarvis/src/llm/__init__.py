from .base import LLMClient, LLMResponse


def make_llm_client(cfg: dict) -> LLMClient:
    """Instantiate the backend named in config.yaml (llm.provider)."""
    provider = cfg["llm"]["provider"]
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM(cfg)
    if provider == "openai_compatible":
        from .openai_compat import OpenAICompatibleLLM

        return OpenAICompatibleLLM(cfg)
    raise ValueError(f"Unknown llm.provider: {provider!r}")
