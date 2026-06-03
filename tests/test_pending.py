from librarian.pending import PendingActions
from tests.conftest import write_note


def test_enqueue_and_list(cfg, vault):
    p = write_note(vault, "n.md", "x")
    q = PendingActions(cfg)
    qid = q.enqueue(str(p), "archive", "Backups", reason="rule_001", source="rule")
    assert qid is not None
    items = q.list()
    assert len(items) == 1 and items[0]["action"] == "archive"
    q.close()


def test_enqueue_dedupes_identical_pending(cfg, vault):
    p = write_note(vault, "n.md", "x")
    q = PendingActions(cfg)
    first = q.enqueue(str(p), "archive", "Backups")
    second = q.enqueue(str(p), "archive", "Backups")
    assert first is not None and second is None
    assert q.count() == 1
    q.close()


def test_approve_reject_transitions(cfg, vault):
    p = write_note(vault, "n.md", "x")
    q = PendingActions(cfg)
    a = q.enqueue(str(p), "archive", "Backups")
    b = q.enqueue(str(p), "move_to_folder", "Daily Notes")
    q.approve(a)
    q.reject(b)
    assert q.count("pending") == 0
    assert q.count("approved") == 1
    assert q.count("rejected") == 1
    q.close()


def test_is_stale_detects_change_and_deletion(cfg, vault):
    p = write_note(vault, "n.md", "original")
    q = PendingActions(cfg)
    q.enqueue(str(p), "archive", "Backups")
    row = q.list()[0]
    assert q.is_stale(row) is False
    p.write_text("CHANGED", encoding="utf-8")  # mtime changes
    assert q.is_stale(row) is True
    p.unlink()
    assert q.is_stale(row) is True
    q.close()
