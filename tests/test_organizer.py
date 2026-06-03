from librarian import organizer
from librarian.pending import PendingActions
from librarian.rules.loader import RulesRegistry
from tests.conftest import write_note

_RULES = """rules:
  - id: rule_001
    name: Backup detection
    type: regex
    pattern: _backup_
    action: flag_as_duplicate
    confidence: 0.95
    hit_count: 0
    created: '2026-01-01'
    description: backups
  - id: rule_002
    name: Daily note placement
    type: regex
    pattern: '^Daily - '
    action: move_to_folder
    target_folder: Daily Notes
    confidence: 0.9
    hit_count: 0
    created: '2026-01-01'
    description: daily notes
"""


def _seed_rules(cfg):
    cfg.rules_registry_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.rules_registry_path.write_text(_RULES)
    return RulesRegistry(cfg.rules_registry_path)


def test_move_rule_auto_applies(cfg, vault):
    write_note(vault, "Daily - 01-01-2026.md", "today")
    reg = _seed_rules(cfg)
    q = PendingActions(cfg)
    summary = organizer.organize(cfg, reg, q, dry_run=False)
    assert summary["auto_applied"] == 1
    assert (vault / "Daily Notes" / "Daily - 01-01-2026.md").exists()
    q.close()


def test_flag_rule_is_queued_not_auto_applied(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "backup")
    reg = _seed_rules(cfg)
    q = PendingActions(cfg)
    summary = organizer.organize(cfg, reg, q, dry_run=False)
    assert summary["queued"] == 1
    assert p.exists()  # NOT moved — only queued
    item = q.list()[0]
    assert item["action"] == "archive" and item["reason"] == "rule_001"
    q.close()


def test_dry_run_writes_nothing_and_queues_nothing(cfg, vault):
    write_note(vault, "Daily - 01-01-2026.md", "today")
    write_note(vault, "x_backup_1.md", "backup")
    reg = _seed_rules(cfg)
    q = PendingActions(cfg)
    summary = organizer.organize(cfg, reg, q, dry_run=True)
    assert summary["auto_applied"] == 1  # previewed
    assert summary["queued"] == 1  # would-queue count
    assert not (vault / "Daily Notes").exists()  # nothing moved
    assert q.count() == 0  # nothing actually enqueued
    q.close()


def test_apply_pending_applies_and_marks_applied(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "backup")
    reg = _seed_rules(cfg)
    q = PendingActions(cfg)
    organizer.organize(cfg, reg, q, dry_run=False)
    aid = q.list()[0]["id"]
    q.approve(aid)
    result = organizer.apply_pending(cfg, q, aid)
    assert "MOVED" in result
    assert (vault / "Archive" / "Backups" / "x_backup_1.md").exists()
    assert not p.exists()
    assert q.count("applied") == 1
    q.close()


def test_apply_pending_skips_stale(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "backup")
    reg = _seed_rules(cfg)
    q = PendingActions(cfg)
    organizer.organize(cfg, reg, q, dry_run=False)
    aid = q.list()[0]["id"]
    q.approve(aid)
    p.write_text("CHANGED SINCE QUEUED", encoding="utf-8")  # make it stale
    result = organizer.apply_pending(cfg, q, aid)
    assert "SKIPPED" in result
    assert p.exists()  # untouched
    assert q.count("failed") == 1
    q.close()
