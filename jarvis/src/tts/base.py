from __future__ import annotations

from abc import ABC, abstractmethod


class TTSEngine(ABC):
    @abstractmethod
    def speak(self, text: str) -> None:
        """Synthesize and play text; blocks until playback ends.

        Blocking by design — the voice loop runs it in an executor.
        """

    def stop(self) -> None:  # noqa: B027 - optional hook (barge-in)
        pass
