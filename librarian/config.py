from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings
from ruamel.yaml import YAML

_yaml = YAML()
_CONFIG_PATH = Path("~/.librarian/config.yaml").expanduser()


class Config(BaseSettings):
    vault_path: str = str(Path("~/Documents/default obsidian vault").expanduser())
    lm_studio_url: str = "http://localhost:1234/v1"
    main_model: str = "qwen/qwen3.6-27b"
    embed_model: str = "text-embedding-nomic-embed-text-v1.5"

    autonomous_mode: bool = True
    debounce_seconds: int = 5

    # Privacy tier assigned to ingested notes that lack a frontmatter privacy_tier.
    # 0=public, 1=personal, 2=sensitive, 3=private. Default to personal for a
    # single-user vault; the Classifier agent elevates sensitive notes to tier 2.
    default_privacy_tier: int = 1
    rule_confidence_threshold: float = 0.85
    rule_generation_batch_size: int = 25
    stale_threshold_days: int = 90
    archive_threshold_days: int = 180

    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Privacy: max tier exposed via external API
    default_external_tier: int = 1

    # Paths (resolved at runtime)
    librarian_dir: Path = Field(default_factory=lambda: Path("~/.librarian").expanduser())

    @property
    def rules_registry_path(self) -> Path:
        return self.librarian_dir / "rules_registry.yaml"

    @property
    def run_log_path(self) -> Path:
        return self.librarian_dir / "run_log.jsonl"

    @property
    def chroma_path(self) -> Path:
        return self.librarian_dir / "chroma"

    @property
    def db_path(self) -> Path:
        return self.librarian_dir / "librarian.db"

    model_config = {"env_prefix": "LIBRARIAN_"}


def load_config() -> Config:
    """Load config from ~/.librarian/config.yaml, falling back to defaults."""
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            data = _yaml.load(f) or {}
        return Config(**data)
    return Config()


def ensure_librarian_dir(cfg: Config) -> None:
    """Create ~/.librarian/ and subdirs if they don't exist."""
    cfg.librarian_dir.mkdir(parents=True, exist_ok=True)
    cfg.chroma_path.mkdir(parents=True, exist_ok=True)
