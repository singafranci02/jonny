from .base import MemoryStore, NullMemory


def make_memory_store(cfg: dict) -> MemoryStore:
    backend = cfg.get("memory", {}).get("backend", "none")
    if backend == "mem0":
        from .mem0_store import Mem0Store

        return Mem0Store(cfg)
    if backend == "none":
        return NullMemory()
    raise ValueError(f"Unknown memory.backend: {backend!r}")
