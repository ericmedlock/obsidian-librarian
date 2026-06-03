"""
Pending-actions queue — the heart of the hybrid-by-risk autonomy model.

Deterministic, safe rule hits are auto-applied; everything ambiguous (LLM
proposals, risky moves) is enqueued here for the user to approve or reject via
`librarian review`. Stored in the same SQLite DB as the graph (separate table).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from librarian.config import Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    note_path   TEXT NOT NULL,
    note_mtime  REAL,            -- file mtime when queued; lets us detect staleness
    action      TEXT NOT NULL,   -- move_to_folder | archive | add_frontmatter
    target      TEXT,            -- folder name, or JSON frontmatter fields
    reason      TEXT,            -- rule id or LLM rationale
    source      TEXT,            -- 'rule' | 'llm'
    status      TEXT NOT NULL DEFAULT 'pending'  -- pending|approved|rejected|applied|failed
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_actions(status);
"""

_OPEN = "pending"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingActions:
    def __init__(self, cfg: Config) -> None:
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self,
        note_path: str,
        action: str,
        target: str = "",
        reason: str = "",
        source: str = "rule",
    ) -> Optional[int]:
        """
        Add a pending action. De-duplicates: if an identical action is already
        pending for the same note, returns None instead of inserting a duplicate.
        """
        mtime = Path(note_path).stat().st_mtime if Path(note_path).exists() else None
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM pending_actions WHERE note_path=? AND action=? "
                "AND target=? AND status=?",
                (note_path, action, target, _OPEN),
            ).fetchone()
            if dup:
                return None
            cur = self._conn.execute(
                "INSERT INTO pending_actions "
                "(created_at, note_path, note_mtime, action, target, reason, source, status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_now(), note_path, mtime, action, target, reason, source, _OPEN),
            )
            self._conn.commit()
            return cur.lastrowid

    def list(self, status: str = _OPEN) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_actions WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, action_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_actions WHERE id=?", (action_id,)
            ).fetchone()
        return dict(row) if row else None

    def _set_status(self, action_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_actions SET status=? WHERE id=?", (status, action_id)
            )
            self._conn.commit()

    def approve(self, action_id: int) -> None:
        self._set_status(action_id, "approved")

    def reject(self, action_id: int) -> None:
        self._set_status(action_id, "rejected")

    def mark_applied(self, action_id: int) -> None:
        self._set_status(action_id, "applied")

    def mark_failed(self, action_id: int) -> None:
        self._set_status(action_id, "failed")

    def is_stale(self, row: dict[str, Any]) -> bool:
        """True if the note changed or vanished since the action was queued —
        such an action should not be applied blindly."""
        p = Path(row["note_path"])
        if not p.exists():
            return True
        if row["note_mtime"] is None:
            return False
        return abs(p.stat().st_mtime - row["note_mtime"]) > 1e-6

    def count(self, status: str = _OPEN) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE status=?", (status,)
            ).fetchone()[0]
