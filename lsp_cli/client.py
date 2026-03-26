"""Client for communicating with the LSP daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from lsp_cli.daemon_state import (
    DAEMON_DIR,
    get_daemon_port,
    release_startup_lock,
    try_acquire_startup_lock,
    wait_for_daemon_port,
)
from lsp_cli.observability import emit_event
from lsp_cli.protocol import Request, read_message


class DaemonClient:
    """Stateless client that sends JSON-RPC requests to the daemon."""

    def __init__(self, auto_start: bool = True) -> None:
        self._auto_start = auto_start
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _ensure_daemon(self) -> int:
        """Get the daemon port, starting it if needed."""
        port = get_daemon_port()
        if port is not None:
            return port

        if not self._auto_start:
            raise ConnectionError(
                "Daemon is not running. Start it with 'lsp daemon start'."
            )

        return self._start_daemon()

    def _start_daemon(self) -> int:
        """Start the daemon process in background and wait for it."""
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        lock_fd = try_acquire_startup_lock()
        if lock_fd is None:
            emit_event("client.daemon_start.wait_for_lock")
            port = wait_for_daemon_port(timeout=30.0)
            if port is not None:
                emit_event("client.daemon_start.reused", port=port)
                return port
            lock_fd = try_acquire_startup_lock()
            if lock_fd is None:
                emit_event("client.daemon_start.lock_timeout")
                raise ConnectionError("Timed out waiting for another lsp daemon startup to finish")

        try:
            existing_port = get_daemon_port()
            if existing_port is not None:
                emit_event("client.daemon_start.already_running", port=existing_port)
                return existing_port

            # Start daemon as a detached subprocess
            python = sys.executable
            cmd = [python, "-m", "lsp_cli.daemon_main"]

            if sys.platform == "win32":
                # Windows: CREATE_NO_WINDOW prevents the console window flash,
                # CREATE_NEW_PROCESS_GROUP detaches from parent's ctrl-C group.
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                CREATE_NO_WINDOW = 0x08000000
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                )
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

            # Wait for daemon to print its port
            if proc.stdout is None:
                raise ConnectionError("Failed to start daemon: no stdout pipe")
            line = proc.stdout.readline().decode("utf-8").strip()
            stderr_output = b""
            if proc.stdout is not None:
                proc.stdout.close()

            try:
                info = json.loads(line)
                port = info["port"]
                emit_event("client.daemon_start.spawned", port=port, output=line)
            except (json.JSONDecodeError, KeyError):
                if proc.stderr is not None:
                    stderr_output = proc.stderr.read()
                    proc.stderr.close()
                stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
                detail = f" stderr: {stderr_text}" if stderr_text else ""
                emit_event("client.daemon_start.failed", output=line, stderr=stderr_text)
                raise ConnectionError(f"Failed to start daemon. Output: {line!r}{detail}")

            # Verify connection
            for _ in range(10):
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1.0)
                        s.connect(("127.0.0.1", port))
                    if proc.stderr is not None:
                        proc.stderr.close()
                    emit_event("client.daemon_start.ready", port=port)
                    return port
                except OSError:
                    time.sleep(0.1)

            emit_event("client.daemon_start.unreachable", port=port)
            raise ConnectionError("Daemon started but not reachable")
        finally:
            release_startup_lock(lock_fd)

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request to the daemon and return the result."""
        start = time.perf_counter()
        port = self._ensure_daemon()
        req = Request(method=method, params=params or {}, id=self._next_id())

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(120.0)
                s.connect(("127.0.0.1", port))
                s.sendall(req.to_bytes())

                buf = bytearray()
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    msg, buf = read_message(bytes(buf))
                    if msg is not None:
                        if "error" in msg and msg["error"]:
                            err = msg["error"]
                            emit_event(
                                "client.call.error",
                                method=method,
                                port=port,
                                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                                error=err.get("message", str(err)),
                            )
                            raise RuntimeError(err.get("message", str(err)))
                        result = msg.get("result")
                        status = result.get("status") if isinstance(result, dict) else None
                        emit_event(
                            "client.call.ok",
                            method=method,
                            port=port,
                            duration_ms=round((time.perf_counter() - start) * 1000, 2),
                            status=status,
                        )
                        return result
                    buf = bytearray(buf)

                emit_event(
                    "client.call.no_response",
                    method=method,
                    port=port,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )
                raise ConnectionError("Daemon closed connection without response")
        except socket.timeout:
            emit_event(
                "client.call.timeout",
                method=method,
                port=port,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            raise ConnectionError(
                f"Daemon did not respond within 120 seconds (method: {method}). "
                "It may have crashed — check 'lsp daemon status'."
            )
