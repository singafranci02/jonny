"""Knowledge index: knowledge/ folder -> chunks -> Ollama embeddings -> Chroma.

A manifest (path -> mtime) makes ingest incremental: only new or changed
files are re-embedded, deleted files are dropped from the index.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT
from .base import KnowledgeIndex

SUPPORTED = {".md", ".txt", ".pdf"}


def read_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(errors="ignore")


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Paragraph-aware chunks of ~size chars with overlap between them."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if current and len(current) + len(p) + 2 > size:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""
        current = f"{current}\n\n{p}" if current else p
        while len(current) > size * 1.5:  # single huge paragraph
            chunks.append(current[:size])
            current = current[size - overlap :]
    if current.strip():
        chunks.append(current)
    return chunks


class ChromaKnowledgeIndex(KnowledgeIndex):
    def __init__(self, cfg: dict):
        import chromadb

        kcfg = cfg["knowledge"]
        self.folder = ROOT / kcfg.get("folder", "knowledge")
        self.top_k = kcfg.get("top_k", 4)
        self.max_distance = kcfg.get("max_distance", 0.75)
        self.chunk_size = kcfg.get("chunk_size", 1500)
        self.chunk_overlap = kcfg.get("chunk_overlap", 200)
        self.embed_model = kcfg.get("embed_model", "nomic-embed-text")
        self.ollama_url = kcfg.get("ollama_url", "http://localhost:11434")

        data_dir = ROOT / kcfg.get("data_dir", "data/knowledge")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = data_dir / "manifest.json"
        client = chromadb.PersistentClient(path=str(data_dir))
        self.collection = client.get_or_create_collection(
            "jarvis_knowledge", metadata={"hnsw:space": "cosine"}
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        import ollama

        client = ollama.Client(host=self.ollama_url)
        return list(client.embed(model=self.embed_model, input=texts).embeddings)

    def _load_manifest(self) -> dict[str, float]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text())
        return {}

    def _save_manifest(self, manifest: dict[str, float]) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=1))

    def remove(self, filename: str) -> None:
        self.collection.delete(where={"source": filename})
        manifest = self._load_manifest()
        if manifest.pop(filename, None) is not None:
            self._save_manifest(manifest)

    def _index_file(self, path: Path, rel: str) -> int:
        text = read_file(path)
        chunks = chunk_text(text, self.chunk_size, self.chunk_overlap)
        self.collection.delete(where={"source": rel})
        if not chunks:
            return 0
        self.collection.add(
            ids=[f"{rel}:{i}" for i in range(len(chunks))],
            documents=chunks,
            embeddings=self._embed(chunks),
            metadatas=[{"source": rel, "chunk": i} for i in range(len(chunks))],
        )
        return len(chunks)

    def ingest(self, force: bool = False) -> dict:
        manifest = self._load_manifest()
        seen: set[str] = set()
        stats = {"indexed": 0, "unchanged": 0, "removed": 0, "chunks": 0}

        for path in sorted(self.folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            rel = str(path.relative_to(self.folder))
            seen.add(rel)
            mtime = path.stat().st_mtime
            if not force and manifest.get(rel) == mtime:
                stats["unchanged"] += 1
                continue
            stats["chunks"] += self._index_file(path, rel)
            stats["indexed"] += 1
            manifest[rel] = mtime

        for rel in [r for r in manifest if r not in seen]:
            self.collection.delete(where={"source": rel})
            del manifest[rel]
            stats["removed"] += 1

        self._save_manifest(manifest)
        return stats

    def search(self, query: str) -> list[tuple[str, str]]:
        if self.collection.count() == 0:
            return []
        result = self.collection.query(
            query_embeddings=self._embed([query]),
            n_results=min(self.top_k, self.collection.count()),
        )
        out: list[tuple[str, str]] = []
        for doc, meta, dist in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            if dist <= self.max_distance:
                out.append((str(meta["source"]), doc))
        return out
