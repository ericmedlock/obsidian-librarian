from __future__ import annotations

from pathlib import Path
from typing import Optional

from librarian.config import Config
from librarian.ingest.chunker import parse_note
from librarian.ingest.embedder import Embedder
from librarian.rag.graph import VaultGraph
from librarian.rag.store import VaultStore
from librarian.rules.engine import FileEvent, RuleEngine, RuleMatch
from librarian.rules.loader import RulesRegistry


class IngestPipeline:
    """
    End-to-end ingest orchestrator: the single place that connects the
    chunker, embedder, vector store, metadata graph, and rule engine.

    Used both by the file watcher (per-event) and by full vault scans.
    """

    def __init__(self, cfg: Config, registry: Optional[RulesRegistry] = None) -> None:
        self.cfg = cfg
        self.embedder = Embedder(cfg)
        self.store = VaultStore(cfg)
        self.graph = VaultGraph(cfg)
        self.rule_engine = RuleEngine(registry) if registry is not None else None

    def ingest_file(self, path: Path) -> int:
        """
        Chunk → embed → upsert to vector store → record metadata in the graph.
        Returns the number of chunks indexed. Skips silently if the file is gone.
        """
        if not path.exists():
            return 0

        fm, body, chunks = parse_note(path)
        embedded = self.embedder.embed_chunks(chunks)
        self.store.upsert(embedded)
        self.graph.upsert_note(path, fm, body)
        return len(embedded)

    def delete_file(self, path: Path) -> None:
        """Remove a file's chunks from the vector store and metadata graph."""
        file_path = str(path)
        self.store.delete(file_path)
        self.graph.delete_note(file_path)

    def apply_rules(self, path: Path) -> Optional[RuleMatch]:
        """
        Run the deterministic rule engine against a file. Returns the first
        matching rule (or None — caller escalates to the LLM agents).
        """
        if self.rule_engine is None or not path.exists():
            return None
        fm, body, _chunks = parse_note(path)
        event = FileEvent(path=path, frontmatter=fm, body=body)
        return self.rule_engine.run(event)

    def close(self) -> None:
        self.graph.close()


def iter_vault_notes(vault_path: str) -> list[Path]:
    """All .md files in the vault, excluding the librarian's own output folder."""
    root = Path(vault_path)
    skip = {"Obsidian Librarian"}
    return [
        p
        for p in root.rglob("*.md")
        if not any(part in skip for part in p.relative_to(root).parts)
    ]
