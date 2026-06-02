from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from librarian.config import Config
from librarian.ingest.chunker import Chunk


@dataclass
class EmbeddedChunk:
    chunk: Chunk
    embedding: list[float]

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "file_path": self.chunk.file_path,
            "heading": self.chunk.heading or "",
            "chunk_index": self.chunk.chunk_index,
            "char_start": self.chunk.char_start,
            "char_end": self.chunk.char_end,
        }


class Embedder:
    def __init__(self, cfg: Config) -> None:
        self._client = OpenAI(base_url=cfg.lm_studio_url, api_key="not-needed")
        self._model = cfg.embed_model

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        if not chunks:
            return []
        texts = [c.text for c in chunks]
        response = self._client.embeddings.create(model=self._model, input=texts)
        return [
            EmbeddedChunk(chunk=chunk, embedding=item.embedding)
            for chunk, item in zip(chunks, response.data)
        ]

    def embed_query(self, text: str) -> list[float]:
        response = self._client.embeddings.create(model=self._model, input=[text])
        return response.data[0].embedding
