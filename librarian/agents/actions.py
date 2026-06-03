"""
Write-action tools for the Organizer and Reporter agents.

SAFETY MODEL (defense in depth — the tools enforce this regardless of what the
LLM decides to do):
  * dry_run=True  → every tool only *describes* what it would do; nothing on
    disk changes. This is enforced here, not just in the prompt.
  * Never delete. Files are only moved (archiving = move into Archive/).
  * All operations are confined to inside the vault; any path that resolves
    outside the vault is refused.
  * Frontmatter writes MERGE — existing user-authored keys are never
    overwritten, and the note body is preserved byte-for-byte.
  * No automatic renaming (renaming a note breaks its inbound wikilinks).
  * Every action (real or previewed) is appended to run_log.jsonl.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from crewai.tools import tool

from librarian.config import Config

_DAILY_RE = ("daily",)  # substrings that mark a daily note (never renamed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_write_tools(cfg: Config, dry_run: bool) -> list:
    """Return the mutating tool set, gated on `dry_run` and confined to the vault."""
    vault = Path(cfg.vault_path).resolve()
    log_path = cfg.run_log_path

    def _inside_vault(p: Path) -> bool:
        try:
            p = p.resolve()
        except OSError:
            return False
        return p == vault or vault in p.parents

    def _log(action: str, **fields) -> None:
        entry = {"timestamp": _now(), "action": action, "dry_run": dry_run, **fields}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _move(src: Path, dest_dir: Path, action: str) -> str:
        if not _inside_vault(src) or not src.exists():
            return f"REFUSED: source not found inside vault: {src}"
        if not _inside_vault(dest_dir):
            return f"REFUSED: destination escapes vault: {dest_dir}"
        dest = dest_dir / src.name
        if dest.resolve() == src.resolve():
            return f"SKIPPED: already in place: {src.name}"
        _log(action, src=str(src), dest=str(dest))
        if dry_run:
            return f"DRY RUN: would move {src.name} → {dest_dir.relative_to(vault)}/"
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return f"REFUSED: destination already exists (won't overwrite): {dest}"
        shutil.move(str(src), str(dest))
        return f"MOVED {src.name} → {dest_dir.relative_to(vault)}/"

    @tool("move_note")
    def move_note(file_path: str, dest_folder: str) -> str:
        """Move a note to `dest_folder` (relative to the vault root), keeping its
        filename. Creates the folder if needed. Never deletes or overwrites."""
        return _move(Path(file_path), (vault / dest_folder), "move")

    @tool("archive_note")
    def archive_note(file_path: str, subfolder: str = "") -> str:
        """Archive a note by moving it into the vault's Archive/ folder (optional
        subfolder, e.g. 'Backups'). Files are moved, never deleted."""
        dest = vault / "Archive" / subfolder if subfolder else vault / "Archive"
        return _move(Path(file_path), dest, "archive")

    @tool("write_frontmatter")
    def write_frontmatter(file_path: str, fields_json: str) -> str:
        """Merge YAML frontmatter into a note. `fields_json` is a JSON object of
        field→value. Existing user-authored keys are preserved (never
        overwritten); the note body is left unchanged."""
        p = Path(file_path)
        if not _inside_vault(p) or not p.exists():
            return f"REFUSED: not found inside vault: {p}"
        try:
            new_fields = json.loads(fields_json)
            if not isinstance(new_fields, dict):
                return "REFUSED: fields_json must be a JSON object"
        except json.JSONDecodeError as exc:
            return f"REFUSED: invalid fields_json: {exc}"

        post = frontmatter.load(str(p))
        added = {k: v for k, v in new_fields.items() if k not in post.metadata}
        if not added:
            return f"SKIPPED: no new fields to add to {p.name}"
        _log("frontmatter", file=str(p), added=added)
        if dry_run:
            return f"DRY RUN: would add {list(added)} to {p.name}"
        post.metadata.update(added)
        p.write_text(frontmatter.dumps(post), encoding="utf-8")
        return f"WROTE frontmatter {list(added)} → {p.name}"

    @tool("write_report")
    def write_report(markdown: str) -> str:
        """Write the vault health report (Markdown) to
        Obsidian Librarian/Reports/<today>.md inside the vault."""
        report_dir = vault / "Obsidian Librarian" / "Reports"
        dest = report_dir / f"{datetime.now(timezone.utc).date().isoformat()}.md"
        _log("report", dest=str(dest), bytes=len(markdown))
        if dry_run:
            return f"DRY RUN: would write {len(markdown)} bytes → {dest.relative_to(vault)}"
        report_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(markdown, encoding="utf-8")
        return f"WROTE report → {dest.relative_to(vault)}"

    return [move_note, archive_note, write_frontmatter, write_report]
