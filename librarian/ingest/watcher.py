from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from librarian.config import Config


class _DebounceHandler(FileSystemEventHandler):
    def __init__(
        self,
        on_upsert: Callable[[Path], None],
        on_delete: Callable[[Path], None],
        debounce_seconds: int,
    ) -> None:
        super().__init__()
        self._on_upsert = on_upsert
        self._on_delete = on_delete
        self._debounce = debounce_seconds
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str, fn: Callable[[], None]) -> None:
        with self._lock:
            if path in self._timers:
                self._timers[path].cancel()
            t = threading.Timer(self._debounce, fn)
            self._timers[path] = t
            t.start()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            p = Path(event.src_path)
            self._schedule(event.src_path, lambda: self._on_upsert(p))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            p = Path(event.src_path)
            self._schedule(event.src_path, lambda: self._on_upsert(p))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and event.src_path.endswith(".md"):
            with self._lock:
                if event.src_path in self._timers:
                    self._timers[event.src_path].cancel()
                    del self._timers[event.src_path]
            self._on_delete(Path(event.src_path))


class VaultWatcher:
    def __init__(
        self,
        cfg: Config,
        on_upsert: Callable[[Path], None],
        on_delete: Callable[[Path], None],
    ) -> None:
        self._vault = cfg.vault_path
        self._handler = _DebounceHandler(on_upsert, on_delete, cfg.debounce_seconds)
        self._observer = Observer()

    def start(self) -> None:
        self._observer.schedule(self._handler, self._vault, recursive=True)
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
