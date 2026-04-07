"""
scanner.py — Walk a repository, parse every supported file, and populate
             the KnowledgeGraph.  Supports full and incremental rescans.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterator

from rich.console import Console
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress,
    SpinnerColumn, TextColumn, TimeElapsedColumn,
)

from .graph import KnowledgeGraph
from .parsers import SUPPORTED_EXTENSIONS, parse_file

console = Console()

# Directories / patterns to always skip
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "node_modules", ".pnp", ".yarn",
    "vendor", "third_party", "extern",
    "dist", "build", "out", ".next", ".nuxt", ".vite",
    ".tox", "venv", ".venv", "env", ".env",
    "coverage", ".nyc_output",
    ".ccg",
})

_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".min.js", ".min.css", ".map",
    ".pb", ".pt", ".pth",            # model weights
    ".pyc", ".pyo",
})


def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".")


def _iter_source_files(root: Path) -> Iterator[Path]:
    """Recursively yield source files that ccg can parse."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in-place to prevent descent
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            # Check compound extensions first (.min.js etc.)
            if any(str(fpath).endswith(bad) for bad in _SKIP_EXTENSIONS):
                continue
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield fpath


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class Scanner:
    """
    Orchestrates full and incremental scans of a repository.

    Parameters
    ----------
    root : Path
        Repository root directory.
    kg : KnowledgeGraph
        Target knowledge graph.
    verbose : bool
        Print extra per-file progress info.
    """

    def __init__(self, root: Path, kg: KnowledgeGraph, verbose: bool = False):
        self.root = root
        self.kg = kg
        self.verbose = verbose

    # ── Public API ────────────────────────────────────────────────────────────

    def full_scan(self) -> dict:
        """
        Walk the entire repository and index every supported file.
        Skips files whose mtime + checksum haven't changed since the
        last scan.
        """
        files = list(_iter_source_files(self.root))
        return self._scan_files(files, label="Full scan")

    def incremental_scan(self, changed_paths: list[Path]) -> dict:
        """
        Re-index only the given paths (e.g. files reported by the
        filesystem watcher or `git diff`).
        """
        # Filter to supported files that exist
        files = [p for p in changed_paths if p.suffix.lower() in SUPPORTED_EXTENSIONS]
        if not files:
            return {"scanned": 0, "skipped": 0, "errors": 0, "elapsed": 0.0}
        return self._scan_files(files, label="Incremental scan")

    def scan_file(self, path: Path) -> bool:
        """
        Index a single file.  Returns True if the file was (re-)indexed.
        """
        rel = _rel(path, self.root)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            # File deleted — remove its symbols
            self.kg.remove_file_symbols(rel)
            return True

        result = parse_file(path, file_path_override=rel)
        if result is None:
            return False

        symbols, edges, language, checksum, loc = result

        if not self.kg.needs_rescan(rel, mtime, checksum):
            return False  # unchanged

        # Remove stale data then insert fresh
        self.kg.remove_file_symbols(rel)
        self.kg.upsert_symbols(symbols)
        self.kg.upsert_edges(edges)
        self.kg.record_file(rel, mtime, checksum, language, loc)
        return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scan_files(self, files: list[Path], label: str) -> dict:
        start = time.monotonic()
        scanned = skipped = errors = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(label, total=len(files))

            for path in files:
                rel = _rel(path, self.root)
                progress.update(task, description=f"{label}: [dim]{rel[-50:]}", advance=1)
                try:
                    indexed = self.scan_file(path)
                    if indexed:
                        scanned += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    if self.verbose:
                        console.print(f"[red]  ✗ {rel}: {exc}[/red]")

        elapsed = time.monotonic() - start
        return {
            "scanned": scanned,
            "skipped": skipped,
            "errors": errors,
            "elapsed": round(elapsed, 2),
        }
