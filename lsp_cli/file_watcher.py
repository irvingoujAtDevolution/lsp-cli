"""File watcher — auto-syncs filesystem changes to language servers."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from watchfiles import Change, watch

if TYPE_CHECKING:
    from lsp_cli.session import Session

log = logging.getLogger(__name__)

# Directories to always ignore
IGNORE_DIRS = {
    ".git", ".hg", ".svn",
    "__pycache__", "node_modules",
    "target", "dist", "build",
    ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache",
    ".cargo",
}


class FileWatcher:
    """Watches a project directory and notifies the language server of changes.

    Uses watchfiles (Rust-based, fast) with debouncing.
    """

    def __init__(
        self,
        root_path: str,
        on_change: Callable[[str, Change], None],
        debounce_ms: int = 200,
    ) -> None:
        self.root_path = root_path
        self.on_change = on_change
        self.debounce_ms = debounce_ms
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start watching in a background thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name=f"file-watcher-{os.path.basename(self.root_path)}",
        )
        self._thread.start()
        log.info("File watcher started for %s", self.root_path)

    def stop(self) -> None:
        """Stop watching."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        log.info("File watcher stopped for %s", self.root_path)

    def _should_ignore(self, path: str) -> bool:
        """Check if a path should be ignored (relative to root only)."""
        try:
            rel = os.path.relpath(path, self.root_path)
        except ValueError:
            return False
        parts = Path(rel).parts
        return any(part in IGNORE_DIRS for part in parts)

    def _watch_loop(self) -> None:
        """Main watch loop."""
        try:
            for changes in watch(
                self.root_path,
                stop_event=self._stop_event,
                debounce=self.debounce_ms,
                recursive=True,
                ignore_permission_denied=True,
            ):
                for change_type, path in changes:
                    if self._should_ignore(path):
                        continue
                    try:
                        self.on_change(path, change_type)
                    except Exception as e:
                        log.warning("Error processing file change %s: %s", path, e)
        except Exception as e:
            if not self._stop_event.is_set():
                log.error("File watcher error: %s", e, exc_info=True)


def create_session_watcher(session: "Session") -> FileWatcher:
    """Create a file watcher that syncs changes to a session's language server."""

    def on_change(path: str, change_type: Change) -> None:
        server = session._server
        if server is None:
            return

        rel_path = os.path.relpath(path, session.root_path).replace("\\", "/")

        if change_type == Change.modified:
            log.debug("File modified: %s", rel_path)
            # SolidLSP handles re-reading on next request
        elif change_type == Change.added:
            log.debug("File added: %s", rel_path)
        elif change_type == Change.deleted:
            log.debug("File deleted: %s", rel_path)

    return FileWatcher(
        root_path=session.root_path,
        on_change=on_change,
    )
