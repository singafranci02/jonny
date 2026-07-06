from .base import KnowledgeIndex, NullKnowledge


def make_knowledge_index(cfg: dict) -> KnowledgeIndex:
    backend = cfg.get("knowledge", {}).get("backend", "chroma")
    if backend == "chroma":
        from .chroma_index import ChromaKnowledgeIndex

        return ChromaKnowledgeIndex(cfg)
    if backend == "none":
        return NullKnowledge()
    raise ValueError(f"Unknown knowledge.backend: {backend!r}")
