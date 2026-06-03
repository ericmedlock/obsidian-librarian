from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML
from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

_yaml = YAML()


@dataclass
class Rule:
    id: str
    name: str
    type: str  # regex | frontmatter_pattern | content_pattern | python_callable
    pattern: str
    action: str
    confidence: float
    hit_count: int
    created: str
    description: str
    target_folder: Optional[str] = None
    created_by: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)


def _parse_rule(raw: dict) -> Rule:
    known = {"id", "name", "type", "pattern", "action", "confidence", "hit_count",
             "created", "description", "target_folder", "created_by"}
    extra = {k: v for k, v in raw.items() if k not in known}
    return Rule(
        id=raw["id"],
        name=raw["name"],
        type=raw["type"],
        pattern=raw["pattern"],
        action=raw["action"],
        confidence=float(raw.get("confidence", 0.0)),
        hit_count=int(raw.get("hit_count", 0)),
        created=str(raw.get("created", "")),
        description=raw.get("description", ""),
        target_folder=raw.get("target_folder"),
        created_by=raw.get("created_by", "unknown"),
        extra=extra,
    )


class RulesRegistry:
    """Loads rules_registry.yaml and keeps it hot-reloaded on file change."""

    def __init__(self, registry_path: Path) -> None:
        self._path = registry_path
        self._lock = threading.RLock()
        self._rules: list[Rule] = []
        self._observer: Optional[Observer] = None
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            data = _yaml.load(f) or {}
        with self._lock:
            self._rules = [_parse_rule(r) for r in (data.get("rules") or [])]

    def start_watching(self) -> None:
        """Hot-reload the registry when the file changes."""
        class _Handler(FileSystemEventHandler):
            def __init__(self_, path: Path) -> None:
                self_._path = path

            def on_modified(self_, event: FileModifiedEvent) -> None:
                if Path(event.src_path) == self_._path:
                    self._load()

        self._observer = Observer()
        self._observer.schedule(_Handler(self._path), str(self._path.parent), recursive=False)
        self._observer.start()

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()

    @property
    def rules(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def increment_hit(self, rule_id: str) -> None:
        """Persist an incremented hit_count back to the YAML file."""
        with self._lock:
            for rule in self._rules:
                if rule.id == rule_id:
                    rule.hit_count += 1
                    break
            self._persist()

    def _persist(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            data = _yaml.load(f) or {"rules": []}
        for raw in data.get("rules") or []:
            for rule in self._rules:
                if raw["id"] == rule.id:
                    raw["hit_count"] = rule.hit_count
        # Atomic write: dump to a temp file in the same dir, then os.replace so a
        # crash or a concurrent reader never sees a half-written registry.
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                _yaml.dump(data, f)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
