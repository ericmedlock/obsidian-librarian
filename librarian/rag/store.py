from __future__ import annotations

from typing import Any, Optional

import chromadb
from chromadb.config import Settings

from librarian.config import Config
from librarian.ingest.embedder import EmbeddedChunk, Embedder


class VaultStore:
    """ChromaDB wrapper for the vault_notes collection."""

    COLLECTION = "vault_notes"

    def __init__(self, cfg: Config) -> None:
        self._client = chromadb.PersistentClient(
            path=str(cfg.chroma_path),
            settings=Settings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection(
            name=self.COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        self._embedder = Embedder(cfg)

    def upsert(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        if not embedded_chunks:
            return
        ids = [f"{ec.chunk.file_path}::{ec.chunk.chunk_index}" for ec in embedded_chunks]
        embeddings = [ec.embedding for ec in embedded_chunks]
        documents = [ec.chunk.text for ec in embedded_chunks]
        metadatas = [ec.metadata for ec in embedded_chunks]
        self._col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    def delete(self, file_path: str) -> None:
        """Remove all chunks belonging to a file."""
        results = self._col.get(where={"file_path": file_path})
        if results["ids"]:
            self._col.delete(ids=results["ids"])

    def query(
        self,
        text: str,
        top_k: int = 10,
        privacy_tier_max: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Embed text, search ChromaDB, filter by privacy_tier.
        Returns list of {text, metadata, distance}.
        """
        embedding = self._embedder.embed_query(text)
        where: Optional[dict] = None
        if privacy_tier_max < 3:
            where = {"privacy_tier": {"$lte": privacy_tier_max}}

        results = self._col.query(
            query_embeddings=[embedding],
            n_results=top_k * 2,  # over-fetch before BM25 re-rank
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        hits = []
        docs = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        dists = results["distances"][0] if results["distances"] else []

        for doc, meta, dist in zip(docs, metas, dists):
            hits.append({"text": doc, "metadata": meta, "distance": dist})

        return hits[:top_k]

    def count(self) -> int:
        return self._col.count()
