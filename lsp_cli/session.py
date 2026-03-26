"""Session management — wraps SolidLanguageServer lifecycle."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import Language, LanguageServerConfig

from lsp_cli.file_watcher import FileWatcher, create_session_watcher
from lsp_cli.observability import emit_event

log = logging.getLogger(__name__)

DEFAULT_STARTUP_BARRIER_TIMEOUT_SECS = 10.0
STARTUP_BARRIER_TIMEOUT_BY_LANGUAGE = {
    Language.RUST: 10.0,
}
STARTUP_BARRIER_ATTRS = (
    "server_ready",
    "analysis_complete",
    "service_ready_event",
)


class SessionStatus(str, Enum):
    STARTING = "starting"
    WARM = "warm"
    READY = "ready"
    ERROR = "error"
    STOPPED = "stopped"


def _startup_barrier_timeout(server: SolidLanguageServer) -> float:
    return STARTUP_BARRIER_TIMEOUT_BY_LANGUAGE.get(server.language, DEFAULT_STARTUP_BARRIER_TIMEOUT_SECS)


def _soften_startup_barriers(server: SolidLanguageServer) -> None:
    """Bound language-specific startup waits so large workspaces become queryable sooner."""
    timeout_secs = _startup_barrier_timeout(server)
    startup_state = getattr(server, "_lsp_cli_startup_state", None)
    if startup_state is None:
        startup_state = {
            "warm": False,
            "fully_ready": False,
            "barrier": None,
            "promotion_started": False,
        }
        setattr(server, "_lsp_cli_startup_state", startup_state)

    for attr_name in STARTUP_BARRIER_ATTRS:
        readiness_event = getattr(server, attr_name, None)
        if readiness_event is None or getattr(readiness_event, "_lsp_cli_patched", False):
            continue
        if not isinstance(readiness_event, threading.Event):
            continue

        original_wait = readiness_event.wait

        def wait_with_timeout(timeout: float | None = None, *, _attr_name: str = attr_name) -> bool:
            effective_timeout = timeout_secs if timeout is None else min(timeout, timeout_secs)
            is_ready = original_wait(effective_timeout)
            if not is_ready:
                startup_state["warm"] = True
                startup_state["barrier"] = _attr_name
                log.warning(
                    "%s did not signal %s within %.1fs; proceeding with a warm session",
                    server.language.value,
                    _attr_name,
                    effective_timeout,
                )
                emit_event(
                    "session.warm",
                    language=server.language.value,
                    timeout_secs=effective_timeout,
                    root_path=getattr(server, "repository_root_path", None),
                    barrier=_attr_name,
                )
                if not startup_state["promotion_started"]:
                    startup_state["promotion_started"] = True

                    def wait_for_full_ready() -> None:
                        original_wait()
                        startup_state["fully_ready"] = True
                        callback = getattr(server, "_lsp_cli_on_fully_ready", None)
                        if callback is not None:
                            callback(_attr_name)

                    threading.Thread(
                        target=wait_for_full_ready,
                        daemon=True,
                        name=f"session-promote-{server.language.value}",
                    ).start()
            else:
                startup_state["fully_ready"] = True
            return is_ready

        setattr(readiness_event, "_lsp_cli_patched", True)
        readiness_event.wait = wait_with_timeout  # type: ignore[method-assign]


@dataclass
class Session:
    """A running language server session for one project."""

    name: str
    root_path: str
    language: Language
    solution: str | None = None
    status: SessionStatus = SessionStatus.STOPPED
    started_at: float | None = None
    _server: SolidLanguageServer | None = field(default=None, repr=False)
    _server_ctx: Any = field(default=None, repr=False)
    _watcher: FileWatcher | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop_requested: bool = field(default=False, repr=False)
    error_message: str | None = None

    @property
    def estimated_ready_seconds(self) -> int:
        """Estimate seconds until ready, based on elapsed time."""
        if self.started_at is None:
            return 15
        elapsed = time.time() - self.started_at
        return max(3, int(30 - elapsed))

    def start_async(self) -> None:
        """Start the language server in a background thread (non-blocking).

        The session should already have status=STARTING set by the caller
        (SessionManager.start_session) before being stored in _sessions,
        to avoid a race where other threads see it as STOPPED.
        """
        with self._lock:
            if self._server is not None:
                return
            if self.status == SessionStatus.STARTING:
                return  # already starting
            self.status = SessionStatus.STARTING
            self.started_at = time.time()
            self.error_message = None
            self._stop_requested = False

        thread = threading.Thread(
            target=self._start_blocking,
            daemon=True,
            name=f"session-start-{self.name}",
        )
        thread.start()

    def _start_blocking(self) -> None:
        """Blocking initialization — runs in background thread."""
        ctx = None
        started = time.perf_counter()
        emit_event("session.starting", name=self.name, root_path=self.root_path, language=self.language.value)
        try:
            # For C#: check for ambiguous solutions before starting
            if self.language == Language.CSHARP and not self.solution:
                self._check_ambiguous_solutions()

            config = LanguageServerConfig(code_language=self.language)
            server = SolidLanguageServer.create(
                config, self.root_path
            )
            setattr(server, "_lsp_cli_on_fully_ready", self._on_server_fully_ready)
            _soften_startup_barriers(server)
            # Patch solution discovery if a specific solution was requested
            if self.solution:
                self._patch_solution_hint(server)
            ctx = server.start_server()
            srv = ctx.__enter__()

            with self._lock:
                # C2 fix: check if stop() was called while we were starting
                if self._stop_requested:
                    log.info("Session %s: stop requested during startup, cleaning up", self.name)
                    self.status = SessionStatus.STOPPED
                    # Must exit the context manager we just entered
                    try:
                        ctx.__exit__(None, None, None)
                    except Exception:
                        pass
                    return

                self._server = srv
                self._server_ctx = ctx
                startup_state = getattr(server, "_lsp_cli_startup_state", {})
                self.status = SessionStatus.WARM if startup_state.get("warm") and not startup_state.get("fully_ready") else SessionStatus.READY

            # Start file watcher (outside lock)
            watcher = create_session_watcher(self)
            watcher.start()
            with self._lock:
                self._watcher = watcher
            log.info("Session %s started for %s (%s)", self.name, self.root_path, self.language.value)
            if self.status == SessionStatus.WARM:
                emit_event(
                    "session.state",
                    name=self.name,
                    root_path=self.root_path,
                    language=self.language.value,
                    status=self.status.value,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    barrier=startup_state.get("barrier"),
                )
            else:
                emit_event(
                    "session.ready",
                    name=self.name,
                    root_path=self.root_path,
                    language=self.language.value,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
        except Exception as e:
            # Clean up context manager if it was entered
            if ctx is not None:
                with self._lock:
                    if self._server_ctx is None:
                        # ctx was entered but not stored — clean it up
                        try:
                            ctx.__exit__(None, None, None)
                        except Exception:
                            pass
            with self._lock:
                self.status = SessionStatus.ERROR
                self.error_message = str(e)
            log.error("Failed to start session %s: %s", self.name, e)
            emit_event(
                "session.error",
                name=self.name,
                root_path=self.root_path,
                language=self.language.value,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=str(e),
            )

    def _on_server_fully_ready(self, barrier: str) -> None:
        with self._lock:
            if self.status != SessionStatus.WARM or self._server is None or self._stop_requested:
                return
            self.status = SessionStatus.READY

        emit_event(
            "session.ready",
            name=self.name,
            root_path=self.root_path,
            language=self.language.value,
            barrier=barrier,
        )

    def _patch_solution_hint(self, server: SolidLanguageServer) -> None:
        """Monkey-patch SolidLSP's solution discovery to return our preferred .sln file.

        SolidLSP's C# language server uses find_solution_or_project_file() in
        create_launch_command() and also does an inline breadth-first scan in
        _open_solution_and_projects(). We patch both so the specified solution
        is consistently used.
        """
        solution_path = os.path.abspath(self.solution)
        if not os.path.isfile(solution_path):
            log.warning("Solution file not found: %s", solution_path)
            return

        try:
            from solidlsp.language_servers import csharp_language_server as cs_mod

            # Patch module-level find_solution_or_project_file (used in create_launch_command)
            def patched_find(root_dir: str) -> str | None:
                log.info("Using specified solution: %s", solution_path)
                return solution_path
            cs_mod.find_solution_or_project_file = patched_find

            # Patch breadth_first_file_scan so _open_solution_and_projects finds
            # our solution first (it scans for .sln/.slnx and .csproj inline)
            original_scan = cs_mod.breadth_first_file_scan

            def patched_scan(root_dir: str):
                # Yield our solution first, then the rest
                yield solution_path
                for f in original_scan(root_dir):
                    if f != solution_path:
                        yield f
            cs_mod.breadth_first_file_scan = patched_scan

            log.info("Patched C# solution discovery → %s", solution_path)
        except (ImportError, AttributeError) as e:
            log.warning("Could not patch solution discovery: %s", e)

    def _check_ambiguous_solutions(self) -> None:
        """Fail early if multiple .sln files exist and none was specified.

        Without this, SolidLSP silently picks the first .sln from a
        breadth-first scan, which is non-deterministic in monorepos.
        """
        root = Path(self.root_path)
        sln_files = sorted(root.glob("*.sln"))
        if len(sln_files) > 1:
            names = ", ".join(f.name for f in sln_files)
            raise RuntimeError(
                f"Multiple .sln files found in {self.root_path}: {names}. "
                f"Use --solution to specify which one, e.g.: "
                f"lsp session start {self.name} --root {self.root_path} "
                f"--lang csharp --solution {sln_files[0]}"
            )

    def stop(self) -> None:
        """Stop the language server and file watcher.

        Safe to call while _start_blocking is running — sets _stop_requested
        flag so the startup thread cleans up after itself.
        """
        with self._lock:
            self._stop_requested = True
            if self._watcher is not None:
                try:
                    self._watcher.stop()
                except Exception as e:
                    log.warning("Error stopping file watcher for %s: %s", self.name, e)
                self._watcher = None
            if self._server_ctx is not None:
                try:
                    self._server_ctx.__exit__(None, None, None)
                except Exception as e:
                    log.warning("Error stopping session %s: %s", self.name, e)
                self._server_ctx = None
                self._server = None
            self.status = SessionStatus.STOPPED
            log.info("Session %s stopped", self.name)
            emit_event("session.stopped", name=self.name, root_path=self.root_path, language=self.language.value)

    @property
    def server(self) -> SolidLanguageServer:
        """Get the underlying language server, raising if not ready."""
        if self._server is None:
            raise RuntimeError(f"Session {self.name!r} is not started")
        return self._server

    def to_dict(self) -> dict:
        """Serialize session info for JSON output."""
        d: dict[str, Any] = {
            "name": self.name,
            "root_path": self.root_path,
            "language": self.language.value,
            "status": self.status.value,
            "error": self.error_message,
        }
        if self.solution:
            d["solution"] = self.solution
        if self.status == SessionStatus.STARTING and self.started_at is not None:
            elapsed = int(time.time() - self.started_at)
            d["elapsed_seconds"] = elapsed
            d["retry_after"] = self.estimated_ready_seconds
        return d


class SessionManager:
    """Manages multiple language server sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def start_session(
        self,
        name: str,
        root_path: str,
        language: str,
        solution: str | None = None,
    ) -> Session:
        """Start a new session (non-blocking) or return existing one.

        Returns immediately. The session will be in STARTING status
        until the language server finishes initialization.

        Args:
            solution: Path to a specific .sln file (for C# monorepos with
                      multiple solutions). If not provided, SolidLSP picks
                      the first .sln it finds.
        """
        with self._lock:
            if name in self._sessions:
                session = self._sessions[name]
                if session.status in (SessionStatus.READY, SessionStatus.WARM, SessionStatus.STARTING):
                    return session
                # Error or stopped — restart
                session.stop()

            lang = Language(language)
            root = os.path.abspath(root_path)
            session = Session(name=name, root_path=root, language=lang, solution=solution)
            self._sessions[name] = session
            # C1 fix: call start_async inside manager lock so the session
            # is never visible to other threads as STOPPED. start_async
            # only sets status + spawns a thread, so holding the lock is fine.
            session.start_async()

        return session

    def stop_session(self, name: str) -> None:
        """Stop and remove a session."""
        with self._lock:
            session = self._sessions.pop(name, None)
        if session:
            session.stop()
        else:
            raise KeyError(f"Session {name!r} not found")

    def get_session(self, name: str) -> Session:
        """Get a session by name."""
        with self._lock:
            session = self._sessions.get(name)
        if session is None:
            raise KeyError(f"Session {name!r} not found")
        return session

    def list_sessions(self) -> list[dict]:
        """List all sessions as dicts."""
        with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    def find_session_for_path(self, path: str) -> Session | None:
        """Auto-route: find the session whose root is the longest prefix of a path.

        Matches both READY and STARTING sessions (so queries return
        indexing status instead of 'no session found').

        Uses case-insensitive comparison on Windows and requires a path
        separator after the root to prevent D:\\foo matching D:\\foobar.
        """
        abs_path = os.path.normcase(os.path.abspath(path))
        best: Session | None = None
        best_len = 0
        with self._lock:
            for session in self._sessions.values():
                if session.status not in (SessionStatus.READY, SessionStatus.WARM, SessionStatus.STARTING):
                    continue
                root = os.path.normcase(session.root_path)
                # Require separator guard: root must be a proper prefix
                if (abs_path == root or abs_path.startswith(root + os.sep)) and len(root) > best_len:
                    best = session
                    best_len = len(root)
        return best

    def find_session_for_file(self, file_path: str) -> Session | None:
        """Backward-compatible wrapper for file-path routing."""
        return self.find_session_for_path(file_path)

    def stop_all(self) -> None:
        """Stop all sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.stop()
            except Exception as e:
                log.warning("Error stopping session %s: %s", session.name, e)
