from librarian.ingest.chunker import parse_note
from librarian.rag.graph import VaultGraph, extract_wikilinks
from tests.conftest import write_note


def _index(cfg, vault):
    g = VaultGraph(cfg)
    for p in vault.rglob("*.md"):
        fm, body, _ = parse_note(p)
        g.upsert_note(p, fm, body)
    return g


def test_wikilink_extraction():
    body = "see [[Note A]] and [[Note B|alias]] and [[Note C#heading]]"
    assert extract_wikilinks(body) == {"Note A", "Note B", "Note C"}


def test_upsert_and_get(cfg, vault):
    write_note(vault, "A.md", "# A", note_type="reference", status="active")
    g = _index(cfg, vault)
    row = g.get(str(vault / "A.md"))
    assert row and row["note_type"] == "reference" and row["status"] == "active"
    g.close()


def test_orphan_detection(cfg, vault):
    write_note(vault, "Hub.md", "links to [[Leaf]]")
    write_note(vault, "Leaf.md", "i am linked")
    write_note(vault, "Lonely.md", "no links in or out")
    g = _index(cfg, vault)
    orphans = {p.split("/")[-1] for p in g.orphans()}
    assert "Lonely.md" in orphans
    assert "Hub.md" not in orphans  # has outgoing link
    assert "Leaf.md" not in orphans  # has incoming link
    g.close()


def test_delete_note_removes_links(cfg, vault):
    write_note(vault, "A.md", "links [[B]]")
    g = _index(cfg, vault)
    g.delete_note(str(vault / "A.md"))
    assert g.get(str(vault / "A.md")) is None
    assert g._conn.execute("SELECT COUNT(*) FROM links WHERE source_path=?", (str(vault / "A.md"),)).fetchone()[0] == 0
    g.close()


def test_prune_missing(cfg, vault):
    a = write_note(vault, "A.md", "a")
    write_note(vault, "B.md", "b")
    g = _index(cfg, vault)
    assert g.count() == 2
    a.unlink()  # delete A from disk
    gone = g.prune_missing()
    assert str(a) in gone
    assert g.count() == 1
    g.close()
