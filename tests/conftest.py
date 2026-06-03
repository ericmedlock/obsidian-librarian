"""
Shared pytest fixtures. Everything here is hermetic: no LM Studio, no network,
no real vault. Embeddings are faked so vector-store paths run offline.
"""

from __future__ import annotations

import hashlib

import pytest

from librarian.config import Config


@pytest.fixture
def cfg(tmp_path):
    """A Config pointing at an isolated temp vault + librarian dir."""
    vault = tmp_path / "vault"
    lib = tmp_path / "lib"
    vault.mkdir()
    lib.mkdir()
    return Config(vault_path=str(vault), librarian_dir=lib)


@pytest.fixture
def vault(cfg):
    """The vault Path for the test config."""
    from pathlib import Path

    return Path(cfg.vault_path)


def _fake_vector(text: str, dim: int = 8) -> list[float]:
    """Deterministic pseudo-embedding so vector tests are reproducible + offline."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:dim]]


@pytest.fixture
def mock_embedder(monkeypatch):
    """Patch the Embedder so embed paths never touch LM Studio."""
    from librarian.ingest import embedder as emb_mod

    def fake_embed_chunks(self, chunks):
        return [
            emb_mod.EmbeddedChunk(
                chunk=c,
                embedding=_fake_vector(c.text),
                default_privacy_tier=self._default_privacy_tier,
            )
            for c in chunks
        ]

    def fake_embed_query(self, text):
        return _fake_vector(text)

    def fake_init(self, cfg):
        self._model = cfg.embed_model
        self._default_privacy_tier = cfg.default_privacy_tier

    monkeypatch.setattr(emb_mod.Embedder, "__init__", fake_init)
    monkeypatch.setattr(emb_mod.Embedder, "embed_chunks", fake_embed_chunks)
    monkeypatch.setattr(emb_mod.Embedder, "embed_query", fake_embed_query)


def write_note(vault, relpath: str, body: str = "body", **frontmatter) -> "Path":  # noqa: F821
    """Helper: write a markdown note (with optional frontmatter) into the vault."""
    from pathlib import Path

    p = Path(vault) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        import yaml  # frontmatter dep brings pyyaml

        fm = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n"
    else:
        fm = ""
    p.write_text(fm + body, encoding="utf-8")
    return p
