from __future__ import annotations

from abc import ABC, abstractmethod


class STTEngine(ABC):
    @abstractmethod
    def listen(self) -> str:
        """Block until one utterance is captured; return its transcript.

        Blocking by design — the voice loop runs it in an executor.
        """

    def shutdown(self) -> None:  # noqa: B027 - optional hook
        pass
