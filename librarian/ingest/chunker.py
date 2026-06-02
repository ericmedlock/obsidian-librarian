from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import frontmatter
import tiktoken

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)
_CHUNK_TOKEN_TARGET = 512
_CHUNK_OVERLAP_TOKENS = 64
_enc = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    text: str
    heading: Optional[str]
    file_path: str
    chunk_index: int
    char_start: int
    char_end: int
    frontmatter: dict = field(default_factory=dict)


def parse_note(path: Path) -> tuple[dict, str, list[Chunk]]:
    """
    Parse a markdown note once and return (frontmatter_dict, body, chunks).
    Splits on H2/H3 headings; falls back to sliding window for headingless notes.
    Frontmatter is not included in any chunk's text.
    """
    raw = path.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)
    fm = dict(post.metadata)
    body = post.content

    chunks = _split_by_headings(body, str(path))
    if not chunks:
        chunks = _sliding_window(body, str(path))

    # Attach the note's frontmatter to every chunk so downstream stages
    # (embedder metadata, graph) can read fields like privacy_tier without
    # re-parsing the file.
    for c in chunks:
        c.frontmatter = fm

    return fm, body, chunks


def chunk_note(path: Path) -> tuple[dict, list[Chunk]]:
    """Backwards-compatible wrapper: return (frontmatter_dict, chunks)."""
    fm, _body, chunks = parse_note(path)
    return fm, chunks


def _split_by_headings(body: str, file_path: str) -> list[Chunk]:
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return []

    sections: list[tuple[Optional[str], int, int]] = []

    # Text before first heading
    if matches[0].start() > 0:
        sections.append((None, 0, matches[0].start()))

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(2).strip(), m.start(), end))

    chunks: list[Chunk] = []
    for idx, (heading, start, end) in enumerate(sections):
        text = body[start:end].strip()
        if not text:
            continue
        chunks.append(Chunk(
            text=text,
            heading=heading,
            file_path=file_path,
            chunk_index=idx,
            char_start=start,
            char_end=end,
        ))
    return chunks


def _sliding_window(body: str, file_path: str) -> list[Chunk]:
    """Fallback: token-based sliding window for notes with no headings."""
    tokens = _enc.encode(body)
    if not tokens:
        return []

    chunks: list[Chunk] = []
    step = _CHUNK_TOKEN_TARGET - _CHUNK_OVERLAP_TOKENS
    idx = 0

    for start_tok in range(0, len(tokens), step):
        end_tok = min(start_tok + _CHUNK_TOKEN_TARGET, len(tokens))
        text = _enc.decode(tokens[start_tok:end_tok])
        # Approximate char positions
        char_start = len(_enc.decode(tokens[:start_tok]))
        char_end = char_start + len(text)
        chunks.append(Chunk(
            text=text,
            heading=None,
            file_path=file_path,
            chunk_index=idx,
            char_start=char_start,
            char_end=char_end,
        ))
        idx += 1
        if end_tok == len(tokens):
            break

    return chunks
