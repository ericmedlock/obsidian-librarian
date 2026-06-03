import json

from librarian.agents.actions import build_write_tools
from librarian.ingest.chunker import parse_note
from librarian.rag.graph import VaultGraph
from tests.conftest import write_note


def tool(tools, name):
    return next(t for t in tools if t.name == name)


def index(cfg, vault):
    g = VaultGraph(cfg)
    for p in vault.rglob("*.md"):
        fm, body, _ = parse_note(p)
        g.upsert_note(p, fm, body)
    g.close()


def test_dry_run_changes_nothing(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "backup")
    tools = build_write_tools(cfg, dry_run=True)
    out = tool(tools, "archive_note").run(file_path=str(p), subfolder="Backups")
    assert "DRY RUN" in out
    assert p.exists()  # untouched
    assert not (vault / "Archive").exists()


def test_live_move_is_a_move_not_copy(cfg, vault):
    p = write_note(vault, "note.md", "body")
    (vault / "Daily Notes").mkdir()
    tools = build_write_tools(cfg, dry_run=False)
    out = tool(tools, "move_note").run(file_path=str(p), dest_folder="Daily Notes")
    assert "MOVED" in out
    assert not p.exists()  # gone from origin
    assert (vault / "Daily Notes" / "note.md").exists()


def test_frontmatter_merge_preserves_user_fields_and_body(cfg, vault):
    p = write_note(vault, "n.md", "# Body\nkeep me", status="active")
    tools = build_write_tools(cfg, dry_run=False)
    tool(tools, "write_frontmatter").run(
        file_path=str(p), fields_json='{"note_type":"reference","status":"OVERWRITE"}'
    )
    text = p.read_text()
    assert "note_type: reference" in text  # new field added
    assert "status: active" in text and "OVERWRITE" not in text  # user value preserved
    assert "keep me" in text  # body preserved


def test_never_deletes_only_archives(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "data")
    tools = build_write_tools(cfg, dry_run=False)
    tool(tools, "archive_note").run(file_path=str(p), subfolder="Backups")
    assert not p.exists()
    assert (vault / "Archive" / "Backups" / "x_backup_1.md").exists()  # moved, not deleted


def test_refuses_path_outside_vault(cfg, vault, tmp_path):
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("secret")
    tools = build_write_tools(cfg, dry_run=False)
    out = tool(tools, "archive_note").run(file_path=str(outside))
    assert "REFUSED" in out
    assert outside.exists()


def test_refuses_overwrite_without_data_loss(cfg, vault):
    p = write_note(vault, "dupe.md", "new")
    (vault / "Archive").mkdir()
    (vault / "Archive" / "dupe.md").write_text("existing")
    tools = build_write_tools(cfg, dry_run=False)
    out = tool(tools, "archive_note").run(file_path=str(p))
    assert "REFUSED" in out
    assert p.exists()  # original not lost


def test_move_updates_graph(cfg, vault):
    p = write_note(vault, "note.md", "# n")
    (vault / "Daily Notes").mkdir()
    index(cfg, vault)
    tools = build_write_tools(cfg, dry_run=False)
    tool(tools, "move_note").run(file_path=str(p), dest_folder="Daily Notes")
    g = VaultGraph(cfg)
    assert g.get(str(vault / "note.md")) is None  # old path gone
    assert g.get(str(vault / "Daily Notes" / "note.md")) is not None  # new path present
    g.close()


def test_frontmatter_updates_graph(cfg, vault):
    p = write_note(vault, "n.md", "# n")
    index(cfg, vault)
    tools = build_write_tools(cfg, dry_run=False)
    tool(tools, "write_frontmatter").run(file_path=str(p), fields_json='{"note_type":"permanent"}')
    g = VaultGraph(cfg)
    assert g.get(str(p))["note_type"] == "permanent"
    g.close()


def test_run_log_records_actions_with_dry_run_flag(cfg, vault):
    p = write_note(vault, "x_backup_1.md", "b")
    tool(build_write_tools(cfg, dry_run=True), "archive_note").run(file_path=str(p), subfolder="Backups")
    tool(build_write_tools(cfg, dry_run=False), "archive_note").run(file_path=str(p), subfolder="Backups")
    entries = [json.loads(line) for line in cfg.run_log_path.read_text().splitlines()]
    flags = [e["dry_run"] for e in entries if e["action"] == "archive"]
    assert True in flags and False in flags
