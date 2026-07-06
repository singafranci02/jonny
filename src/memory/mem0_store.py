"""Mem0 OSS backend: fact extraction via Claude Haiku, local embeddings
via Ollama (nomic-embed-text), vectors persisted in Chroma under data/.

Mem0 decides ADD/UPDATE/DELETE per fact, so stale facts get superseded
instead of duplicated.
"""

from __future__ import annotations

import os

from ..config import ROOT
from .base import MemoryStore


class Mem0Store(MemoryStore):
    def __init__(self, cfg: dict):
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        from mem0 import Memory

        mcfg = cfg["memory"]
        data_dir = ROOT / mcfg.get("data_dir", "data/mem0")
        data_dir.mkdir(parents=True, exist_ok=True)

        self.user_id = mcfg.get("user_id", "francesco")
        self.top_k = mcfg.get("top_k", 5)
        self.memory = Memory.from_config(
            {
                "llm": {
                    "provider": "anthropic",
                    "config": {
                        "model": mcfg.get("extraction_llm", "claude-haiku-4-5"),
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    },
                },
                "embedder": {
                    "provider": "ollama",
                    "config": {
                        "model": mcfg.get("embed_model", "nomic-embed-text"),
                        "ollama_base_url": mcfg.get(
                            "ollama_url", "http://localhost:11434"
                        ),
                    },
                },
                "vector_store": {
                    "provider": "chroma",
                    "config": {
                        "collection_name": "jarvis_memory",
                        "path": str(data_dir),
                    },
                },
                "history_db_path": str(data_dir / "history.db"),
            }
        )

    @staticmethod
    def _results(raw) -> list[dict]:
        if isinstance(raw, dict):
            return raw.get("results", [])
        return raw or []

    def search(self, query: str) -> list[str]:
        raw = self.memory.search(
            query, top_k=self.top_k, filters={"user_id": self.user_id}
        )
        return [r["memory"] for r in self._results(raw) if r.get("memory")]

    def add_turn(self, user_message: str, assistant_message: str) -> None:
        self.memory.add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id=self.user_id,
        )

    def list_all(self) -> list[dict]:
        raw = self.memory.get_all(filters={"user_id": self.user_id}, top_k=100)
        return [
            {"id": r.get("id", "?"), "memory": r.get("memory", "")}
            for r in self._results(raw)
        ]

    def add_fact(self, fact: str) -> None:
        self.memory.add(
            [{"role": "user", "content": f"Remember this: {fact}"}],
            user_id=self.user_id,
        )

    def forget(self, memory_id: str) -> None:
        self.memory.delete(memory_id)

    def forget_all(self) -> None:
        self.memory.delete_all(user_id=self.user_id)
