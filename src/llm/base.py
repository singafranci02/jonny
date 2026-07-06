"""Provider-agnostic LLM interface. Every backend implements chat()."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    extra: dict = field(default_factory=dict)


class LLMClient(ABC):
    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list[dict],
        tier: str = "default",
    ) -> LLMResponse:
        """messages: [{"role": "user"|"assistant", "content": str}, ...]"""
