from pathlib import Path

from librarian import safety
from librarian.agents.actions import build_write_tools
from librarian.ingest.chunker import parse_note
from librarian.rag.graph import VaultGraph
from tests.conftest import write_note


def _index(cfg, vault):
    g = VaultGraph(cfg)
    for p in vault.rglob("*.md"):
        fm, body, _ = parse_note(p)
        g.upsert_note(p, fm, body)
    g.close()


def test_name_based_link_is_not_a_breaker(vault):
    src = Path(vault) / "sub" / "Target.md"
    # name-based inbound link survives a move
    links = [("/v/Other.md", "Target")]
    assert safety.breaking_inbound_links(links, vault, src) == []


def test_path_based_link_to_current_location_is_a_breaker(vault):
    src = Path(vault) / "sub" / "Target.md"
    links = [("/v/Other.md", "sub/Target")]
    breakers = safety.breaking_inbound_links(links, vault, src)
    assert breakers == [("/v/Other.md", "sub/Target")]


def test_path_link_to_different_location_not_a_breaker(vault):
    src = Path(vault) / "sub" / "Target.md"
    links = [("/v/Other.md", "elsewhere/Target")]
    assert safety.breaking_inbound_links(links, vault, src) == []


def test_move_refuses_when_path_link_would_break(cfg, vault):
    write_note(vault, "sub/Target.md", "i am linked by path")
    write_note(vault, "Other.md", "see [[sub/Target]]")
    (vault / "Archive").mkdir()
    _index(cfg, vault)
    tools = build_write_tools(cfg, dry_run=False)
    out = next(t for t in tools if t.name == "archive_note").run(file_path=str(vault / "sub" / "Target.md"))
    assert "REFUSED" in out and "wikilink" in out
    assert (vault / "sub" / "Target.md").exists()  # not moved


def test_move_proceeds_with_only_name_based_links(cfg, vault):
    write_note(vault, "sub/Target.md", "linked by name only")
    write_note(vault, "Other.md", "see [[Target]]")
    (vault / "Daily Notes").mkdir()
    _index(cfg, vault)
    tools = build_write_tools(cfg, dry_run=False)
    out = next(t for t in tools if t.name == "move_note").run(
        file_path=str(vault / "sub" / "Target.md"), dest_folder="Daily Notes"
    )
    assert "MOVED" in out
    assert (vault / "Daily Notes" / "Target.md").exists()
