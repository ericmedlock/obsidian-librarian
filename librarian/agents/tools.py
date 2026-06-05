"""
Read-only vault inspection tools for the CrewAI agents.

These are deliberately *summary-oriented*: they return metrics, filtered lists,
and single-note reads rather than dumping the whole 2,300-note vault into the
model's context. They are backed by the SQLite metadata graph (fast, no
embedding needed) plus the deterministic rule engine.
"""

from __future__ import annotations

import json
from pathlib import Path

from crewai.tools import tool
from librarian.config import Config
from librarian.ingest.chunker import parse_note
from librarian.pipeline import iter_vault_notes
from librarian.rag.graph import VaultGraph
from librarian.rules.engine import FileEvent, RuleEngine
from librarian.rules.loader import RulesRegistry

_MAX_NOTE_CHARS = 4000


def build_vault_tools(cfg: Config, registry: RulesRegistry) -> list:
    """Return the read-only tool set, each closed over the live config."""

    @tool("vault_stats")
    def vault_stats() -> str:
        """Overall vault metrics: total notes, orphan count, total wikilinks.
        Use this first to understand the scale and health of the vault."""
        g = VaultGraph(cfg)
        stats = g.stats()
        g.close()
        return json.dumps(stats)

    @tool("list_orphan_notes")
    def list_orphan_notes(limit: int = 50) -> str:
        """List note filenames that have no incoming or outgoing wikilinks
        (candidates for archiving or linking). Returns up to `limit` names."""
        g = VaultGraph(cfg)
        orphans = g.orphans()
        g.close()
        return json.dumps([Path(p).name for p in orphans[:limit]])

    @tool("list_notes_missing_frontmatter")
    def list_notes_missing_frontmatter(limit: int = 50) -> str:
        """List notes whose note_type or status frontmatter is empty — the
        notes the Classifier should enrich. Returns up to `limit` file paths."""
        g = VaultGraph(cfg)
        rows = g._conn.execute(
            "SELECT file_path FROM notes WHERE note_type='' OR status='' LIMIT ?",
            (limit,),
        ).fetchall()
        g.close()
        return json.dumps([r[0] for r in rows])

    @tool("read_note")
    def read_note(file_path: str) -> str:
        """Read a single note's frontmatter and body text (truncated). Pass the
        full file path as returned by the other tools."""
        p = Path(file_path)
        if not p.exists():
            return f"ERROR: file not found: {file_path}"
        fm, body, _chunks = parse_note(p)
        body = body[:_MAX_NOTE_CHARS]
        return json.dumps({"frontmatter": {k: str(v) for k, v in fm.items()}, "body": body})

    @tool("run_rule_engine")
    def run_rule_engine(limit: int = 0) -> str:
        """Run the deterministic rule registry across the whole vault and return
        every rule hit (file, rule_id, action). This is the cheap, token-free
        first pass — always run it before reasoning about individual notes.
        limit=0 means scan all notes."""
        engine = RuleEngine(registry)
        notes = iter_vault_notes(cfg.vault_path)
        if limit:
            notes = notes[:limit]
        hits = []
        for note in notes:
            try:
                fm, body, _ = parse_note(note)
            except Exception:  # noqa: BLE001 — skip unreadable notes
                continue
            match = engine.run(FileEvent(path=note, frontmatter=fm, body=body), count_hit=False)
            if match:
                hits.append(
                    {"file": note.name, "rule_id": match.rule.id, "action": match.action}
                )
        # Cap the payload so it can't bloat the model's context across retries.
        return json.dumps(
            {"scanned": len(notes), "total_hits": len(hits), "hits": hits[:200]}
        )

    return [
        vault_stats,
        list_orphan_notes,
        list_notes_missing_frontmatter,
        read_note,
        run_rule_engine,
    ]
