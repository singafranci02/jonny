"""Provider-agnostic LLM interface. Every backend implements chat()."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: str
    stop_reason: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    extra: dict = field(default_factory=dict)


class LLMClient(ABC):
    """One provider backend. `messages` uses the provider's native format
    inside a turn (tool exchanges); across turns only plain-text
    user/assistant strings are persisted, so providers stay swappable.
    """

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list[dict],
        model_cfg: dict,
        tier: str = "default",
        tools: list | None = None,
        on_text=None,
    ) -> LLMResponse:
        """tools: registry Tool objects (see src/tools).
        on_text: optional callback fed text deltas as they stream in
        (used for sentence-streaming TTS); the full response is still
        returned at the end either way.
        """

    @abstractmethod
    def tool_result_messages(
        self, response: LLMResponse, results: list[tuple[ToolCall, str, bool]]
    ) -> list[dict]:
        """Provider-format messages for the assistant tool-call turn plus
        its results. results: (call, output, is_error)."""
