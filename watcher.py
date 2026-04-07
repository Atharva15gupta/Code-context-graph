"""
watcher.py — Real-time filesystem watcher that triggers incremental
             rescans whenever a tracked source file changes.

Uses the `watchdog` library which delegates to OS-native APIs:
  inotify (Linux), FSEvents (macOS), ReadDirectoryChangesW (Windows).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from rich.console import Console

from .graph import KnowledgeGraph
from .parsers import SUPPORTED_EXTENSIONS
from .scanner import Scanner

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

console = Console()


class _CodeContextGraphHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Debounced event handler — batches rapid changes into single rescans."""

    DEBOUNCE_SECONDS = 0.8

    def __init__(self, root: Path, kg: KnowledgeGraph):
        super().__init__()
        self.root = root
        self.scanner = Scanner(root, kg)
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # watchdog callbacks ──────────────────────────────────────────────────────

    def on_modified(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._enqueue(event.src_path)
        self._enqueue(event.dest_path)

    # Debouncing ──────────────────────────────────────────────────────────────

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        # Skip hidden dirs and node_modules, etc.
        if any(part.startswith(".") or part == "node_modules" for part in path.parts):
            return

        with self._lock:
            self._pending.add(path)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.DEBOUNCE_SECONDS, self._flush)
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            paths = list(self._pending)
            self._pending.clear()

        if not paths:
            return

        console.print(
            f"[bold cyan]⟳  ccg:[/bold cyan] rescanning {len(paths)} file(s)…"
        )
        result = self.scanner.incremental_scan(paths)
        console.print(
            f"[green]✓[/green] Indexed [bold]{result['scanned']}[/bold] file(s) "
            f"in {result['elapsed']}s"
        )


class Watcher:
    """
    Start / stop the filesystem watcher.

    Usage
    -----
    >>> watcher = Watcher(root, kg)
    >>> watcher.start()         # non-blocking
    >>> watcher.wait()          # blocks until Ctrl-C
    >>> watcher.stop()
    """

    def __init__(self, root: Path, kg: KnowledgeGraph):
        if not _WATCHDOG_AVAILABLE:
            raise RuntimeError(
                "The `watchdog` package is required for --watch mode. "
                "Install it with:  pip install watchdog"
            )
        self.root = root
        self._handler = _CodeContextGraphHandler(root, kg)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(root), recursive=True)

    def start(self) -> None:
        self._observer.start()
        console.print(
            f"[bold green]👁  Watching[/bold green] [cyan]{self.root}[/cyan] "
            f"for changes (Ctrl-C to stop)…"
        )

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def wait(self) -> None:
        """Block until the user sends SIGINT (Ctrl-C)."""
        try:
            while self._observer.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
