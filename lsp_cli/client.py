"""Client for communicating with the LSP daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from lsp_cli.daemon import DAEMON_DIR, PORT_FILE, get_daemon_port
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
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Wait for daemon to print its port
        if proc.stdout is None:
            raise ConnectionError("Failed to start daemon: no stdout pipe")
        line = proc.stdout.readline().decode("utf-8").strip()
        proc.stdout.close()

        try:
            info = json.loads(line)
            port = info["port"]
        except (json.JSONDecodeError, KeyError):
            raise ConnectionError(f"Failed to start daemon. Output: {line!r}")

        # Verify connection
        for _ in range(10):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(("127.0.0.1", port))
                return port
            except OSError:
                time.sleep(0.1)

        raise ConnectionError("Daemon started but not reachable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request to the daemon and return the result."""
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
                            raise RuntimeError(err.get("message", str(err)))
                        return msg.get("result")
                    buf = bytearray(buf)

                raise ConnectionError("Daemon closed connection without response")
        except socket.timeout:
            raise ConnectionError(
                f"Daemon did not respond within 120 seconds (method: {method}). "
                "It may have crashed — check 'lsp daemon status'."
            )
