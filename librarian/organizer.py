"""
Deterministic organizer — the hybrid-by-risk core.

Walks the vault, runs the rule engine, and for each hit either:
  * AUTO-APPLIES it (safe, deterministic, reversible — currently folder moves,
    which are link-guarded by the write tools), or
  * ENQUEUES it for human approval (anything we shouldn't do unattended —
    e.g. archiving a flagged duplicate, per the user's preference).

The same `apply_action` mapping is used by the auto-apply path and by
`librarian review` when a queued action is approved, so there's one source of
truth for how an action maps to a write tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from librarian.agents.actions import build_write_tools
from librarian.config import Config
from librarian.ingest.chunker import parse_note
from librarian.pending import PendingActions
from librarian.pipeline import iter_vault_notes
from librarian.rules.engine import FileEvent, RuleEngine, RuleMatch
from librarian.rules.loader import RulesRegistry


@dataclass
class Plan:
    action: str        # move_to_folder | archive | add_frontmatter
    target: str        # folder name, subfolder, or JSON fields
    auto: bool         # True → auto-apply; False → queue for approval


def plan_for_match(match: RuleMatch) -> Optional[Plan]:
    """Translate a rule hit into an executable plan + its risk disposition."""
    action = match.action
    if action == "move_to_folder" and match.target_folder:
        # Deterministic, reversible, link-guarded → safe to auto-apply.
        return Plan("move_to_folder", match.target_folder, auto=True)
    if action == "flag_as_duplicate":
        # Don't auto-archive (the user prefers to confirm); suggest it.
        return Plan("archive", "Backups", auto=False)
    # Unknown / not-yet-mapped actions are queued, never auto-applied.
    return None


def apply_action(write_tools: list, action: str, note_path: str, target: str) -> str:
    """Single source of truth: run one action via the appropriate write tool."""
    by_name = {t.name: t for t in write_tools}
    if action == "move_to_folder":
        return by_name["move_note"].run(file_path=note_path, dest_folder=target)
    if action == "archive":
        return by_name["archive_note"].run(file_path=note_path, subfolder=target)
    if action == "add_frontmatter":
        return by_name["write_frontmatter"].run(file_path=note_path, fields_json=target)
    return f"REFUSED: unknown action '{action}'"


def _applied_ok(result: str) -> bool:
    return result.startswith(("MOVED", "WROTE", "DRY RUN"))


def organize(
    cfg: Config,
    registry: RulesRegistry,
    pending: PendingActions,
    dry_run: bool = True,
    limit: int = 0,
) -> dict:
    """
    Run the deterministic rule pass over the vault.

    Returns a summary: scanned, auto_applied, queued, skipped, and per-item
    detail. In dry_run, nothing is written and nothing is enqueued — auto items
    are previewed and queueable items are only counted.
    """
    engine = RuleEngine(registry)
    write_tools = build_write_tools(cfg, dry_run)
    notes = iter_vault_notes(cfg.vault_path)
    if limit:
        notes = notes[:limit]

    summary = {"scanned": 0, "auto_applied": 0, "queued": 0, "skipped": 0, "detail": []}
    for note in notes:
        summary["scanned"] += 1
        try:
            fm, body, _ = parse_note(note)
        except Exception:  # noqa: BLE001 — skip unreadable notes
            continue
        match = engine.run(FileEvent(path=note, frontmatter=fm, body=body))
        if not match:
            continue
        plan = plan_for_match(match)
        if plan is None:
            continue

        if plan.auto:
            result = apply_action(write_tools, plan.action, str(note), plan.target)
            if _applied_ok(result):
                summary["auto_applied"] += 1
            else:
                summary["skipped"] += 1  # refused (e.g. link-breaking) or skipped
            summary["detail"].append({"note": note.name, "auto": True, "result": result})
        else:
            if dry_run:
                summary["queued"] += 1  # would queue
            else:
                qid = pending.enqueue(
                    str(note), plan.action, plan.target, reason=match.rule.id, source="rule"
                )
                if qid is not None:
                    summary["queued"] += 1
                else:
                    summary["skipped"] += 1  # already queued (dedup)
            summary["detail"].append(
                {"note": note.name, "auto": False, "action": plan.action, "target": plan.target}
            )

    return summary


def apply_pending(cfg: Config, pending: PendingActions, action_id: int) -> str:
    """Apply one approved pending action via the write tools (live)."""
    row = pending.get(action_id)
    if row is None:
        return f"REFUSED: no pending action #{action_id}"
    if pending.is_stale(row):
        pending.mark_failed(action_id)
        return f"SKIPPED: note changed since queued ({Path(row['note_path']).name})"
    write_tools = build_write_tools(cfg, dry_run=False)
    result = apply_action(write_tools, row["action"], row["note_path"], row["target"])
    if _applied_ok(result):
        pending.mark_applied(action_id)
    else:
        pending.mark_failed(action_id)
    return result
