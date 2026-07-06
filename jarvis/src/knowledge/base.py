from __future__ import annotations

from abc import ABC, abstractmethod


class KnowledgeIndex(ABC):
    """RAG over the knowledge/ folder. Sync by design (run in executors)."""

    @abstractmethod
    def search(self, query: str) -> list[tuple[str, str]]:
        """Top chunks for the query as (source_file, chunk_text)."""

    @abstractmethod
    def ingest(self, force: bool = False) -> dict:
        """(Re)index new/changed files; returns stats for the CLI."""

    @abstractmethod
    def remove(self, filename: str) -> None:
        """Drop a deleted file's chunks from the index."""


class NullKnowledge(KnowledgeIndex):
    def search(self, query: str) -> list[tuple[str, str]]:
        return []

    def ingest(self, force: bool = False) -> dict:
        return {}

    def remove(self, filename: str) -> None:
        pass
