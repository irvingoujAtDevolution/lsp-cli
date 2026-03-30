"""Session management — wraps SolidLanguageServer lifecycle."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

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
PROGRESS_STALE_AFTER_SECS = 30.0
PROGRESS_NOTIFICATION_METHODS = (
    "$/progress",
    "experimental/serverStatus",
    "language/status",
    "window/logMessage",
    "workspace/projectInitializationComplete",
    "custom/file-indexed",
    "textDocument/publishDiagnostics",
)
_FILE_PATH_HINT_RE = re.compile(r"([A-Za-z]:\\\\[^\s:]+|(?:[^\\/\s]+[/\\\\])+[^\\/\s]+\.[A-Za-z0-9_]+)")
_PROGRESS_COUNT_RE = re.compile(r"(?P<completed>\d+)\s*/\s*(?P<total>\d+)")


class SessionStatus(str, Enum):
    STARTING = "starting"
    WARM = "warm"
    READY = "ready"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class SessionProgress:
    phase: str = "unknown"
    title: str | None = None
    message: str | None = None
    percentage: float | int | None = None
    current_file: str | None = None
    observed_files: int | None = None
    completed: int | None = None
    total: int | None = None
    source: str | None = None
    token: str | None = None
    last_update_time: float | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self, include_raw: bool = False) -> dict[str, Any] | None:
        has_payload = any(
            value is not None
            for value in (
                self.title,
                self.message,
                self.percentage,
                self.current_file,
                self.observed_files,
                self.completed,
                self.total,
                self.source,
                self.token,
                self.raw,
            )
        ) or self.phase != "unknown"
        if not has_payload:
            return None

        data: dict[str, Any] = {"phase": self.phase}
        if self.title:
            data["title"] = self.title
        if self.message:
            data["message"] = self.message
        if self.percentage is not None:
            data["percentage"] = self.percentage
        if self.current_file:
            data["current_file"] = self.current_file
        if self.observed_files is not None:
            data["observed_files"] = self.observed_files
        if self.completed is not None:
            data["completed"] = self.completed
        if self.total is not None:
            data["total"] = self.total
        if self.source:
            data["source"] = self.source
        if self.token:
            data["token"] = self.token
        if self.last_update_time is not None:
            age = max(0.0, time.time() - self.last_update_time)
            data["last_update_ts"] = datetime.fromtimestamp(self.last_update_time, timezone.utc).isoformat()
            data["stale"] = age > PROGRESS_STALE_AFTER_SECS
        if include_raw and self.raw is not None:
            data["raw"] = self.raw
        return data


def _startup_barrier_timeout(server: SolidLanguageServer) -> float:
    return STARTUP_BARRIER_TIMEOUT_BY_LANGUAGE.get(server.language, DEFAULT_STARTUP_BARRIER_TIMEOUT_SECS)


def _normalize_percentage(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _uri_to_path(uri: str) -> str | None:
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return os.path.abspath(url2pathname(parsed.path))


def _display_path(path: str | None, root_path: str) -> str | None:
    if not path:
        return None
    abs_path = os.path.abspath(path)
    root_abs = os.path.abspath(root_path)
    root_norm = os.path.normcase(root_abs)
    path_norm = os.path.normcase(abs_path)
    if path_norm == root_norm or path_norm.startswith(root_norm + os.sep):
        return os.path.relpath(abs_path, root_abs).replace("\\", "/")
    return abs_path


def _extract_file_hint(text: str | None, root_path: str) -> str | None:
    if not text:
        return None
    match = _FILE_PATH_HINT_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip().rstrip(".,)")
    if candidate.startswith("file://"):
        return _display_path(_uri_to_path(candidate), root_path)
    if os.path.exists(candidate):
        return _display_path(candidate, root_path)
    return candidate.replace("\\", "/")


def _extract_progress_counts(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    match = _PROGRESS_COUNT_RE.search(text)
    if not match:
        return None
    return int(match.group("completed")), int(match.group("total"))


def _infer_phase(text: str | None, default: str = "unknown") -> str:
    if not text:
        return default
    lowered = text.lower()
    if any(term in lowered for term in ("initialize", "initializing", "starting", "boot")):
        return "initializing"
    if any(term in lowered for term in ("workspace", "project", "solution", "restore", "metadata", "loading")):
        return "loading_workspace"
    if any(term in lowered for term in ("index", "indexing", "cache", "discover", "scan")):
        return "indexing"
    if any(term in lowered for term in ("analy", "check", "diagnostic", "validate", "compile", "proc macro")):
        return "analyzing"
    if any(term in lowered for term in ("ready", "quiescent", "idle", "complete", "completed", "done")):
        return "idle"
    return default


def _normalize_progress_notification(method: str, params: Any, root_path: str) -> dict[str, Any] | None:
    raw = {"method": method, "params": params}
    if method == "$/progress" and isinstance(params, dict):
        value = params.get("value")
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        title = value.get("title")
        message = value.get("message")
        percentage = _normalize_percentage(value.get("percentage"))
        counts = _extract_progress_counts(message or title)
        token = str(params.get("token")) if params.get("token") is not None else None
        phase = _infer_phase(title or message or token, default="progress")
        if phase == "progress" and (counts is not None or percentage is not None):
            phase = "indexing"
        if kind == "end":
            phase = "idle"
            if message is None and title:
                message = f"{title} complete"
        elif kind == "begin" and message is None and title:
            message = title
        completed = counts[0] if counts is not None else None
        total = counts[1] if counts is not None else None
        return {
            "phase": phase,
            "title": title,
            "message": message,
            "percentage": percentage,
            "completed": completed,
            "total": total,
            "current_file": _extract_file_hint(message or title, root_path),
            "source": "lsp-progress",
            "token": token,
            "raw": raw,
        }

    if method == "experimental/serverStatus" and isinstance(params, dict):
        message = params.get("message")
        quiescent = params.get("quiescent")
        title = params.get("health")
        phase = "idle" if quiescent is True else _infer_phase(message or title, default="indexing")
        return {
            "phase": phase,
            "title": title,
            "message": message,
            "source": "server-status",
            "raw": raw,
        }

    if method == "language/status" and isinstance(params, dict):
        title = params.get("type")
        message = params.get("message")
        ready = title == "ServiceReady" and message == "ServiceReady"
        return {
            "phase": "idle" if ready else _infer_phase(message or title, default="initializing"),
            "title": title,
            "message": message,
            "source": "server-status",
            "raw": raw,
        }

    if method == "window/logMessage" and isinstance(params, dict):
        message = params.get("message")
        if not message:
            return None
        return {
            "phase": _infer_phase(message, default="log"),
            "message": message,
            "current_file": _extract_file_hint(message, root_path),
            "source": "window-log",
            "raw": raw,
        }

    if method == "workspace/projectInitializationComplete":
        return {
            "phase": "idle",
            "message": "Project initialization complete",
            "source": "server-status",
            "raw": raw,
        }

    if method == "custom/file-indexed" and isinstance(params, dict):
        file_path = params.get("file") or params.get("path") or _uri_to_path(params.get("uri", ""))
        return {
            "phase": "indexing",
            "message": "Indexed file",
            "current_file": _display_path(file_path, root_path),
            "source": "custom-file-indexed",
            "raw": raw,
        }

    if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
        file_path = _uri_to_path(params.get("uri", ""))
        if not file_path:
            return None
        return {
            "phase": "analyzing",
            "message": "Diagnostics updated",
            "current_file": _display_path(file_path, root_path),
            "source": "diagnostics",
            "raw": raw,
        }

    return None


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
    _progress: SessionProgress = field(default_factory=SessionProgress, repr=False)
    _seen_files: set[str] = field(default_factory=set, repr=False)
    _last_progress_event_key: tuple[Any, ...] | None = field(default=None, repr=False)
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
            self._progress = SessionProgress(
                phase="initializing",
                message="Starting language server",
                source="session",
                last_update_time=time.time(),
            )
            self._seen_files.clear()
            self._last_progress_event_key = None

        thread = threading.Thread(
            target=self._start_blocking,
            daemon=True,
            name=f"session-start-{self.name}",
        )
        thread.start()

    def _attach_progress_handlers(self, server: SolidLanguageServer) -> None:
        process = server.server
        if getattr(process, "_lsp_cli_progress_patched", False):
            return

        original_on_notification = process.on_notification

        def on_notification(method: str, cb: Any) -> None:
            if method in PROGRESS_NOTIFICATION_METHODS:
                def wrapped(params: Any) -> None:
                    self._handle_server_notification(method, params)
                    cb(params)

                original_on_notification(method, wrapped)
                return

            original_on_notification(method, cb)

        process.on_notification = on_notification  # type: ignore[method-assign]
        setattr(process, "_lsp_cli_progress_patched", True)

    def _handle_server_notification(self, method: str, params: Any) -> None:
        try:
            progress_update = _normalize_progress_notification(method, params, self.root_path)
            if progress_update is None:
                return

            emit_payload: dict[str, Any] | None = None
            with self._lock:
                current_file = progress_update.get("current_file")
                if current_file:
                    self._seen_files.add(current_file)
                    progress_update["observed_files"] = len(self._seen_files)
                elif self._seen_files:
                    progress_update["observed_files"] = len(self._seen_files)

                changed = False
                for key, value in progress_update.items():
                    if getattr(self._progress, key) != value:
                        setattr(self._progress, key, value)
                        changed = True
                if not changed:
                    return

                self._progress.last_update_time = time.time()
                snapshot = self._progress.to_dict(include_raw=False)
                if snapshot is None:
                    return
                event_key = (
                    snapshot.get("phase"),
                    snapshot.get("message"),
                    snapshot.get("title"),
                    snapshot.get("percentage"),
                    snapshot.get("current_file"),
                    snapshot.get("observed_files"),
                    snapshot.get("source"),
                    snapshot.get("token"),
                )
                if event_key == self._last_progress_event_key:
                    return
                self._last_progress_event_key = event_key
                emit_payload = snapshot

            emit_event(
                "session.progress",
                name=self.name,
                root_path=self.root_path,
                language=self.language.value,
                status=self.status.value,
                method=method,
                progress=emit_payload,
            )
        except Exception:
            log.debug("Ignoring progress notification handling failure for %s", method, exc_info=True)

    def progress_snapshot(self, include_raw: bool = False) -> dict[str, Any] | None:
        with self._lock:
            return self._progress.to_dict(include_raw=include_raw)

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
            self._attach_progress_handlers(server)
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
                if self.status == SessionStatus.WARM:
                    if self._progress.phase in {"unknown", "idle"}:
                        self._progress.phase = "loading_workspace"
                    if not self._progress.message:
                        self._progress.message = "Session is queryable while indexing continues"
                    self._progress.source = self._progress.source or "session"
                    self._progress.last_update_time = time.time()
                else:
                    self._progress.phase = "idle"
                    self._progress.message = "Session ready"
                    self._progress.source = "session"
                    self._progress.last_update_time = time.time()

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
                self._progress.phase = "error"
                self._progress.message = str(e)
                self._progress.source = "session"
                self._progress.last_update_time = time.time()
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
            self._progress.phase = "idle"
            self._progress.message = f"Server reported ready via {barrier}"
            self._progress.source = "session"
            self._progress.last_update_time = time.time()

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
            self._progress.phase = "stopped"
            self._progress.message = "Session stopped"
            self._progress.source = "session"
            self._progress.last_update_time = time.time()
            log.info("Session %s stopped", self.name)
            emit_event("session.stopped", name=self.name, root_path=self.root_path, language=self.language.value)

    @property
    def server(self) -> SolidLanguageServer:
        """Get the underlying language server, raising if not ready."""
        if self._server is None:
            raise RuntimeError(f"Session {self.name!r} is not started")
        return self._server

    def to_dict(self, include_progress_raw: bool = False) -> dict:
        """Serialize session info for JSON output."""
        d: dict[str, Any] = {
            "name": self.name,
            "root_path": self.root_path,
            "language": self.language.value,
            "status": self.status.value,
            "error": self.error_message,
        }
        progress = self.progress_snapshot(include_raw=include_progress_raw)
        if progress is not None:
            d["progress"] = progress
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
            if session.language == Language.CSHARP and not session.solution:
                session._check_ambiguous_solutions()
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

    def list_sessions(self, include_progress_raw: bool = False) -> list[dict]:
        """List all sessions as dicts."""
        with self._lock:
            sessions = list(self._sessions.values())

        serialized: list[dict] = []
        for session in sessions:
            try:
                serialized.append(session.to_dict(include_progress_raw=include_progress_raw))
            except Exception as e:
                log.warning("Failed to serialize session %s: %s", getattr(session, "name", "<unknown>"), e)
                language = getattr(session, "language", None)
                serialized.append(
                    {
                        "name": getattr(session, "name", "<unknown>"),
                        "root_path": getattr(session, "root_path", ""),
                        "language": language.value if isinstance(language, Language) else str(language),
                        "status": "error",
                        "error": f"Failed to serialize session: {e}",
                    }
                )
        return serialized

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
