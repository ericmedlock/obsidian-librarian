from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from librarian.config import Config

# [[Target]], [[Target|alias]], [[Target#heading]], [[Target#heading|alias]]
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    file_path     TEXT PRIMARY KEY,
    title         TEXT,
    modified_at   REAL,
    word_count    INTEGER,
    note_type     TEXT,
    status        TEXT,
    topic         TEXT,
    privacy_tier  INTEGER,
    tags          TEXT          -- JSON array
);
CREATE TABLE IF NOT EXISTS links (
    source_path TEXT NOT NULL,   -- file_path of the note containing the link
    target_name TEXT NOT NULL,   -- wikilink target (note stem, no extension)
    PRIMARY KEY (source_path, target_name)
);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_name);
CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(note_type);
"""


def extract_wikilinks(body: str) -> set[str]:
    """Return the set of distinct wikilink targets (note stems) in a note body."""
    return {m.group(1).strip() for m in _WIKILINK_RE.finditer(body) if m.group(1).strip()}


class VaultGraph:
    """SQLite metadata + wikilink-edge store for the vault."""

    def __init__(self, cfg: Config) -> None:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the watcher fires callbacks from debounce-timer
        # threads, so the connection is shared across threads. All access is
        # serialized through self._lock to keep that safe.
        self._conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Map a stored file_path to its wikilink-comparable stem ("/a/My Note.md" -> "My Note").
        self._conn.create_function("_stem", 1, lambda p: Path(p).stem, deterministic=True)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._default_privacy_tier = cfg.default_privacy_tier

    def close(self) -> None:
        self._conn.close()

    def upsert_note(self, path: Path, frontmatter: dict[str, Any], body: str) -> None:
        """Insert/replace a note's metadata and rebuild its outgoing links."""
        file_path = str(path)
        tags = frontmatter.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        privacy_tier = frontmatter.get("privacy_tier", self._default_privacy_tier)
        try:
            privacy_tier = int(privacy_tier)
        except (TypeError, ValueError):
            privacy_tier = self._default_privacy_tier

        modified_at = path.stat().st_mtime if path.exists() else None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO notes
                    (file_path, title, modified_at, word_count, note_type, status,
                     topic, privacy_tier, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    title=excluded.title, modified_at=excluded.modified_at,
                    word_count=excluded.word_count, note_type=excluded.note_type,
                    status=excluded.status, topic=excluded.topic,
                    privacy_tier=excluded.privacy_tier, tags=excluded.tags
                """,
                (
                    file_path,
                    str(frontmatter.get("title", path.stem)),
                    modified_at,
                    len(body.split()),
                    str(frontmatter.get("note_type", "")),
                    str(frontmatter.get("status", "")),
                    str(frontmatter.get("topic", "")),
                    privacy_tier,
                    json.dumps(tags),
                ),
            )

            # Rebuild outgoing links for this note.
            self._conn.execute("DELETE FROM links WHERE source_path = ?", (file_path,))
            targets = extract_wikilinks(body)
            if targets:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO links (source_path, target_name) VALUES (?, ?)",
                    [(file_path, t) for t in targets],
                )
            self._conn.commit()

    def delete_note(self, file_path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM notes WHERE file_path = ?", (file_path,))
            self._conn.execute("DELETE FROM links WHERE source_path = ?", (file_path,))
            self._conn.commit()

    def get(self, file_path: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM notes WHERE file_path = ?", (file_path,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def orphans(self) -> list[str]:
        """
        Notes with no incoming and no outgoing links.

        A note has an incoming link if any other note links to its stem; an
        outgoing link if it contains at least one wikilink.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT n.file_path
                FROM notes n
                WHERE NOT EXISTS (
                    SELECT 1 FROM links l WHERE l.source_path = n.file_path
                )
                AND NOT EXISTS (
                    SELECT 1 FROM links l
                    WHERE l.target_name = _stem(n.file_path)
                )
                """
            ).fetchall()
        return [r[0] for r in rows]

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = self.count()
            links = self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            orphans = len(self.orphans())
        return {"total_notes": total, "orphans": orphans, "total_links": links}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
    return d
