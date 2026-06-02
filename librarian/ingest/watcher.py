from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from librarian.config import Config


class _DebounceHandler(FileSystemEventHandler):
    """
    Coalesces filesystem events per path and resolves them by existence.

    Every create/modify/delete/move on a `.md` path (re)schedules a single
    debounced "settle". When the timer fires we check whether the file still
    exists: present → upsert, gone → delete. This is essential because editors
    (including Obsidian) save atomically (write-temp + rename-over), which emits
    a delete event for the real path; handling deletes eagerly would wipe the
    index on every save instead of re-indexing.
    """

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

    def _settle(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        p = Path(path)
        if p.exists():
            self._on_upsert(p)
        else:
            self._on_delete(p)

    def _schedule(self, path: str) -> None:
        if not path.endswith(".md"):
            return
        with self._lock:
            if path in self._timers:
                self._timers[path].cancel()
            t = threading.Timer(self._debounce, self._settle, args=(path,))
            self._timers[path] = t
            t.start()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)  # old path: will resolve to delete
            self._schedule(event.dest_path)  # new path: will resolve to upsert


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
