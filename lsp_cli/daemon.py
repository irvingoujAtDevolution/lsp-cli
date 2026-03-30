"""LSP CLI Daemon — long-running background process managing language server sessions.

Uses TCP on localhost (cross-platform) for CLI <-> daemon communication.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from lsp_cli.daemon_state import DAEMON_DIR, LOG_FILE, PID_FILE, PORT_FILE, is_daemon_running
from lsp_cli.observability import emit_event
from lsp_cli.protocol import Request, Response, make_error, read_message
from lsp_cli.session import SessionManager, SessionStatus

log = logging.getLogger(__name__)


class DaemonServer:
    """TCP server that accepts CLI connections and dispatches LSP operations."""

    def __init__(self) -> None:
        self.session_manager = SessionManager()
        self._server_socket: socket.socket | None = None
        self._running = False

    def start(self, port: int = 0) -> int:
        """Start the daemon server. Returns the actual port."""
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)

        actual_port = self._server_socket.getsockname()[1]

        # Write PID and port files
        PID_FILE.write_text(str(os.getpid()))
        PORT_FILE.write_text(str(actual_port))

        self._running = True
        log.info("Daemon listening on 127.0.0.1:%d (pid=%d)", actual_port, os.getpid())
        return actual_port

    def serve_forever(self) -> None:
        """Accept and handle connections until stopped."""
        assert self._server_socket is not None

        while self._running:
            try:
                client, addr = self._server_socket.accept()
                t = threading.Thread(
                    target=self._handle_client,
                    args=(client,),
                    daemon=True,
                )
                t.start()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    raise
                break

    def stop(self) -> None:
        """Stop the daemon and clean up."""
        self._running = False
        self.session_manager.stop_all()
        if self._server_socket:
            self._server_socket.close()
        PID_FILE.unlink(missing_ok=True)
        PORT_FILE.unlink(missing_ok=True)
        log.info("Daemon stopped")

    def _handle_client(self, client: socket.socket) -> None:
        """Handle a single client connection (one request-response cycle)."""
        try:
            client.settimeout(120.0)
            buf = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buf += chunk
                msg, buf = read_message(buf)
                if msg is not None:
                    response = self._dispatch(msg)
                    client.sendall(response.to_bytes())
                    break
        except Exception as e:
            log.error("Client handler error: %s", e, exc_info=True)
        finally:
            client.close()

    def _dispatch(self, msg: dict[str, Any]) -> Response:
        """Dispatch a JSON-RPC request to the appropriate handler."""
        req_id = msg.get("id", 0)
        method = msg.get("method", "")
        params = msg.get("params", {})
        started = time.perf_counter()

        handlers = {
            "session/start": self._handle_session_start,
            "session/stop": self._handle_session_stop,
            "session/list": self._handle_session_list,
            "session/info": self._handle_session_info,
            "lsp/definition": self._handle_definition,
            "lsp/references": self._handle_references,
            "lsp/hover": self._handle_hover,
            "lsp/symbols": self._handle_symbols,
            "lsp/diagnostics": self._handle_diagnostics,
            "lsp/outline": self._handle_outline,
            "lsp/rename": self._handle_rename,
            "daemon/shutdown": self._handle_shutdown,
            "daemon/status": self._handle_status,
        }

        handler = handlers.get(method)
        if handler is None:
            emit_event("daemon.dispatch.missing", method=method, request_id=req_id)
            return make_error(req_id, -32601, f"Method not found: {method}")

        try:
            result = handler(params)
            emit_event(
                "daemon.dispatch.ok",
                method=method,
                request_id=req_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status=result.get("status") if isinstance(result, dict) else None,
                session=params.get("session"),
                file=params.get("file"),
                root=params.get("root"),
            )
            return Response(id=req_id, result=result)
        except KeyError as e:
            emit_event(
                "daemon.dispatch.key_error",
                method=method,
                request_id=req_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=str(e),
            )
            return make_error(req_id, -32602, str(e))
        except Exception as e:
            log.error("Handler error for %s: %s", method, e, exc_info=True)
            emit_event(
                "daemon.dispatch.error",
                method=method,
                request_id=req_id,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=str(e),
            )
            return make_error(req_id, -32000, str(e))

    # --- Session handlers ---

    def _handle_session_start(self, params: dict) -> dict:
        name = params["name"]
        root = params["root"]
        lang = params["language"]
        solution = params.get("solution")
        session = self.session_manager.start_session(name, root, lang, solution=solution)
        return session.to_dict()  # Returns immediately (non-blocking)

    def _check_session_ready(self, session: Any) -> dict | None:
        """Return an indexing/error response if session isn't ready, or None if ready."""
        if session.status == SessionStatus.STARTING:
            return {"status": "indexing", **session.to_dict()}
        if session.status == SessionStatus.ERROR:
            return {"status": "error", "message": session.error_message}
        return None

    def _handle_session_stop(self, params: dict) -> dict:
        self.session_manager.stop_session(params["name"])
        return {"status": "stopped"}

    def _handle_session_list(self, params: dict) -> list:
        return self.session_manager.list_sessions()

    def _handle_session_info(self, params: dict) -> dict:
        session = self.session_manager.get_session(params["name"])
        return session.to_dict(include_progress_raw=True)

    # --- LSP query handlers ---

    def _resolve_session(self, params: dict) -> Any:
        """Resolve session from params — by name, file path auto-routing, or auto-creation.

        Priority:
        1. Explicit --session name
        2. Existing session matching file path
        3. Auto-create new session by detecting project root + language
        """
        session_name = params.get("session")
        file_path = params.get("file")
        root_path = params.get("root")

        if session_name:
            return self.session_manager.get_session(session_name)

        if file_path:
            # Try existing session first
            session = self.session_manager.find_session_for_path(file_path)
            if session:
                return session

            # Auto-create: detect root and language
            detection = _auto_detect_root(file_path)
            if detection:
                root, lang_hint = detection
                lang = _auto_detect_language(file_path, lang_hint)
                if lang:
                    name = self._generate_session_name(root)
                    log.info("Auto-creating session %r for %s (%s)", name, root, lang)
                    return self.session_manager.start_session(name, root, lang)

        if root_path:
            session = self.session_manager.find_session_for_path(root_path)
            if session:
                return session

            detection = _auto_detect_root(root_path)
            if detection:
                root, lang_hint = detection
                lang = _auto_detect_language(root_path, lang_hint)
                if lang:
                    name = self._generate_session_name(root)
                    log.info("Auto-creating session %r for %s (%s)", name, root, lang)
                    return self.session_manager.start_session(name, root, lang)

        raise KeyError(
            "No session specified and could not auto-detect project root/language. "
            "Use --session or start a session with 'lsp session start'."
        )

    def _generate_session_name(self, root: str) -> str:
        """Generate a unique session name from the project root directory."""
        base = os.path.basename(root)
        if not base:
            base = "project"
        name = base
        counter = 2
        existing = {s["name"] for s in self.session_manager.list_sessions()}
        while name in existing:
            name = f"{base}-{counter}"
            counter += 1
        return name

    def _handle_definition(self, params: dict) -> list[dict] | dict:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        rel_path = _to_relative(params["file"], session.root_path)
        line = params["line"]
        col = params["col"]

        locations = server.request_definition(rel_path, line, col)
        return [_format_location(loc, session.root_path) for loc in locations]

    def _handle_references(self, params: dict) -> list[dict] | dict:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        rel_path = _to_relative(params["file"], session.root_path)
        line = params["line"]
        col = params["col"]

        locations = server.request_references(rel_path, line, col)
        return [_format_location(loc, session.root_path) for loc in locations]

    def _handle_hover(self, params: dict) -> dict | None:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        rel_path = _to_relative(params["file"], session.root_path)
        line = params["line"]
        col = params["col"]

        hover = server.request_hover(rel_path, line, col)
        if hover is None:
            return None
        return _format_hover(hover)

    def _handle_symbols(self, params: dict) -> list[dict] | dict:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        query = params.get("query", "")

        symbols = server.request_full_symbol_tree()
        # Filter by query if provided
        if query:
            symbols = _filter_symbol_tree(symbols, query)

        return [_format_symbol(s) for s in symbols]

    def _handle_diagnostics(self, params: dict) -> list[dict] | dict:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        file_path = params.get("file")

        if file_path:
            rel_path = _to_relative(file_path, session.root_path)
            diags = server.request_text_document_diagnostics(rel_path)
            return [_format_diagnostic(d, rel_path) for d in diags]
        return []

    def _handle_outline(self, params: dict) -> list[dict] | dict:
        session = self._resolve_session(params)
        not_ready = self._check_session_ready(session)
        if not_ready:
            return not_ready
        server = session.server
        rel_path = _to_relative(params["file"], session.root_path)

        symbols = server.request_document_overview(rel_path)
        return [_format_symbol(s) for s in symbols]

    def _handle_rename(self, params: dict) -> dict:
        raise NotImplementedError("Rename is not yet implemented in daemon mode")

    # --- Daemon management ---

    def _handle_shutdown(self, params: dict) -> dict:
        threading.Thread(target=self._delayed_stop, daemon=True).start()
        return {"status": "shutting_down"}

    def _handle_status(self, params: dict) -> dict:
        return {
            "pid": os.getpid(),
            "sessions": self.session_manager.list_sessions(),
        }

    def _delayed_stop(self) -> None:
        time.sleep(0.5)
        self.stop()


# --- Auto-detection helpers ---

# Root markers ordered by specificity (language-specific first, then generic)
ROOT_MARKERS_BY_LANG = {
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pyproject.toml": "python",
    "setup.py": "python",
    "package.json": "typescript",
    "tsconfig.json": "typescript",
    "composer.json": "php",
    "pom.xml": "java",
    "build.gradle": "java",
}
ROOT_MARKERS_GENERIC = [".git", ".hg", ".svn"]

EXTENSION_TO_LANG = {
    ".rs": "rust",
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".h": "cpp",
    ".c": "c",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".php": "php",
    ".lua": "lua",
    ".zig": "zig",
}


def _auto_detect_root(file_path: str) -> tuple[str, str | None] | None:
    """Detect project root and language from file path.

    Walks up from file looking for language-specific markers (Cargo.toml, etc.)
    then generic markers (.git).

    Returns (root_path, language_hint) or None.
    """
    abs_path = os.path.abspath(file_path)
    directory = os.path.dirname(abs_path) if os.path.isfile(abs_path) or not os.path.exists(abs_path) else abs_path

    current = directory
    while True:
        # Check language-specific markers first
        for marker, lang in ROOT_MARKERS_BY_LANG.items():
            if os.path.exists(os.path.join(current, marker)):
                return (current, lang)
        # Check for *.sln / *.csproj
        try:
            for entry in os.scandir(current):
                if entry.name.endswith(".sln") or entry.name.endswith(".csproj"):
                    return (current, "csharp")
        except OSError:
            pass
        # Check generic markers
        for marker in ROOT_MARKERS_GENERIC:
            if os.path.exists(os.path.join(current, marker)):
                return (current, None)

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def _auto_detect_language(file_path: str, root_hint: str | None = None) -> str | None:
    """Detect language from file extension, with optional root-based hint override."""
    if root_hint:
        return root_hint
    ext = os.path.splitext(file_path)[1].lower()
    return EXTENSION_TO_LANG.get(ext)


# --- Constants ---

SYMBOL_KIND_NAMES = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor",
    10: "Enum", 11: "Interface", 12: "Function", 13: "Variable",
    14: "Constant", 15: "String", 16: "Number", 17: "Boolean",
    18: "Array", 19: "Object", 20: "Key", 21: "Null",
    22: "EnumMember", 23: "Struct", 24: "Event", 25: "Operator",
    26: "TypeParameter",
}

# --- Formatting helpers ---

def _to_relative(file_path: str, root: str) -> str:
    """Convert an absolute or CWD-relative path to project-relative."""
    abs_path = os.path.abspath(file_path)
    abs_root = os.path.abspath(root)
    try:
        rel = os.path.relpath(abs_path, abs_root)
        # Normalize separators for LSP
        return rel.replace("\\", "/")
    except ValueError:
        return file_path


def _format_location(loc: dict, root: str) -> dict:
    """Format an LSP Location dict for CLI output."""
    rel_path = _extract_location_path(loc, root)
    range_info = loc.get("range", {})
    start = range_info.get("start", {})

    result: dict[str, Any] = {
        "file": rel_path,
        "line": start.get("line", 0) + 1,
        "col": start.get("character", 0) + 1,
    }

    # Add preview if available
    if "preview" in loc:
        result["preview"] = loc["preview"]

    return result


def _extract_location_path(loc: dict[str, Any], root: str) -> str:
    """Extract the best available path from a backend location payload."""
    for key in ("relativePath", "relative_path", "file"):
        value = loc.get(key)
        if isinstance(value, str) and value:
            return value.replace("\\", "/")

    for key in ("absolutePath", "path"):
        value = loc.get(key)
        if isinstance(value, str) and value:
            return _to_relative(value, root)

    uri = loc.get("uri")
    if isinstance(uri, str) and uri:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            abs_path = url2pathname(parsed.path)
            if os.name == "nt" and abs_path.startswith("/") and len(abs_path) > 2 and abs_path[2] == ":":
                abs_path = abs_path[1:]
            return _to_relative(abs_path, root)

    return ""


def _filter_symbol_tree(symbols: list[Any], query: str) -> list[Any]:
    """Recursively filter a symbol tree while preserving matching descendants."""
    query_lower = query.lower()
    matches: list[Any] = []

    for symbol in symbols:
        if not isinstance(symbol, dict):
            continue

        children = symbol.get("children", [])
        matched_children = _filter_symbol_tree(children, query) if isinstance(children, list) else []
        name = symbol.get("name", "")
        name_matches = isinstance(name, str) and query_lower in name.lower()

        if name_matches or matched_children:
            filtered = dict(symbol)
            if matched_children:
                filtered["children"] = matched_children
            elif "children" in filtered:
                filtered.pop("children")
            matches.append(filtered)

    return matches


def _format_hover(hover: dict) -> dict:
    """Format hover result."""
    contents = hover.get("contents", "")
    if isinstance(contents, dict):
        # MarkedString or MarkupContent
        value = contents.get("value", str(contents))
        language = contents.get("language", contents.get("kind", ""))
        return {"contents": value, "language": language}
    elif isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, dict):
                parts.append(item.get("value", str(item)))
            else:
                parts.append(str(item))
        return {"contents": "\n---\n".join(parts)}
    return {"contents": str(contents)}


def _format_symbol(sym: Any) -> dict:
    """Format a UnifiedSymbolInformation for CLI output.

    UnifiedSymbolInformation is a TypedDict (dict subclass), so use dict access.
    """
    if isinstance(sym, dict):
        result: dict[str, Any] = {"name": sym.get("name", "")}
        kind = sym.get("kind")
        if kind is not None:
            result["kind"] = SYMBOL_KIND_NAMES.get(kind, f"Unknown({kind})")
        location = sym.get("location", {})
        if isinstance(location, dict):
            rel_path = location.get("relativePath", location.get("relative_path", ""))
            if rel_path:
                result["file"] = rel_path
            range_info = location.get("range", {})
            start = range_info.get("start", {}) if isinstance(range_info, dict) else {}
            if start:
                result["line"] = start.get("line", 0) + 1
                result["col"] = start.get("character", 0) + 1
        children = sym.get("children", [])
        if children:
            result["children"] = [_format_symbol(c) for c in children]
        return result
    else:
        raise TypeError(f"Unexpected symbol type: {type(sym)}")


def _format_diagnostic(diag: dict, file_path: str) -> dict:
    """Format a diagnostic for CLI output."""
    range_info = diag.get("range", {})
    start = range_info.get("start", {})

    severity_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    severity = severity_map.get(diag.get("severity", 0), "unknown")

    return {
        "file": file_path,
        "line": start.get("line", 0) + 1,
        "col": start.get("character", 0) + 1,
        "severity": severity,
        "message": diag.get("message", ""),
        "source": diag.get("source", ""),
    }


def run_daemon(foreground: bool = False) -> None:
    """Start the daemon process."""
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_FILE)),
            *([] if not foreground else [logging.StreamHandler()]),
        ],
    )

    daemon = DaemonServer()

    def handle_signal(signum: int, frame: Any) -> None:
        log.info("Received signal %d, shutting down", signum)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    port = daemon.start()
    print(json.dumps({"status": "started", "port": port, "pid": os.getpid()}))
    sys.stdout.flush()

    daemon.serve_forever()
