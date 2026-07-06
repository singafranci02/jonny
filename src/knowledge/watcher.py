"""Watch knowledge/ and re-index automatically when files change."""

from __future__ import annotations

import threading

from .base import KnowledgeIndex


def start_watcher(index: KnowledgeIndex, folder) -> object | None:
    """Debounced watchdog observer; returns it (call .stop()) or None."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

    class Handler(FileSystemEventHandler):
        def __init__(self) -> None:
            self._timer: threading.Timer | None = None

        def on_any_event(self, event) -> None:
            if event.is_directory:
                return
            # debounce: editors fire several events per save
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(2.0, index.ingest)
            self._timer.daemon = True
            self._timer.start()

    observer = Observer()
    observer.schedule(Handler(), str(folder), recursive=True)
    observer.daemon = True
    observer.start()
    return observer
