from __future__ import annotations

from abc import ABC, abstractmethod


class MemoryStore(ABC):
    """Long-term memory: durable facts across sessions.

    All methods are synchronous by design — the voice loop runs them in
    executors so retrieval/extraction never block audio.
    """

    @abstractmethod
    def search(self, query: str) -> list[str]:
        """Top memories relevant to the query, as plain sentences."""

    @abstractmethod
    def add_turn(self, user_message: str, assistant_message: str) -> None:
        """Extract durable facts from one exchange and upsert them."""

    @abstractmethod
    def list_all(self) -> list[dict]:
        """[{'id': ..., 'memory': ...}, ...] for the CLI."""

    @abstractmethod
    def add_fact(self, fact: str) -> None:
        """Manually store a fact (CLI)."""

    @abstractmethod
    def forget(self, memory_id: str) -> None:
        """Delete one memory by id (CLI)."""

    @abstractmethod
    def forget_all(self) -> None:
        """Delete everything (CLI)."""


class NullMemory(MemoryStore):
    """memory.backend: none — the assistant runs stateless."""

    def search(self, query: str) -> list[str]:
        return []

    def add_turn(self, user_message: str, assistant_message: str) -> None:
        pass

    def list_all(self) -> list[dict]:
        return []

    def add_fact(self, fact: str) -> None:
        pass

    def forget(self, memory_id: str) -> None:
        pass

    def forget_all(self) -> None:
        pass
