from pathlib import Path

from librarian.ingest.chunker import chunk_note, parse_note
from tests.conftest import write_note


def test_splits_on_headings(vault):
    p = write_note(vault, "n.md", "## One\nalpha\n\n## Two\nbeta")
    _fm, _body, chunks = parse_note(p)
    headings = [c.heading for c in chunks]
    assert "One" in headings and "Two" in headings
    assert len(chunks) >= 2


def test_frontmatter_stripped_from_body_but_attached(vault):
    p = write_note(vault, "n.md", "## H\ncontent", title="T", note_type="reference")
    fm, body, chunks = parse_note(p)
    assert fm["note_type"] == "reference"
    assert "note_type" not in body  # frontmatter not in body text
    assert all(c.frontmatter.get("note_type") == "reference" for c in chunks)


def test_malformed_frontmatter_does_not_raise(vault):
    # A colon in an unquoted value breaks YAML; parse_note must fall back, not crash.
    p = Path(vault) / "bad.md"
    p.write_text("---\ntitle: a: b: c\n---\n\nreal body here", encoding="utf-8")
    fm, body, chunks = parse_note(p)
    assert "real body here" in body
    assert chunks  # still produced chunks


def test_headingless_note_uses_sliding_window(vault):
    p = write_note(vault, "flat.md", "just one paragraph with no headings at all")
    _fm, chunks = chunk_note(p)
    assert len(chunks) >= 1
    assert chunks[0].heading is None
