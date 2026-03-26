"""Lightweight daemon state helpers shared by client and daemon."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

# Default paths
DAEMON_DIR = Path.home() / ".lsp-cli"
PID_FILE = DAEMON_DIR / "daemon.pid"
PORT_FILE = DAEMON_DIR / "daemon.port"
LOG_FILE = DAEMON_DIR / "daemon.log"
STARTUP_LOCK_FILE = DAEMON_DIR / "daemon.start.lock"


def get_daemon_port() -> int | None:
    """Read the daemon port from the port file, or None if not running."""
    if not PORT_FILE.exists():
        return None
    try:
        port = int(PORT_FILE.read_text().strip())
        # Check if daemon is actually listening
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(("127.0.0.1", port))
        return port
    except (ValueError, OSError):
        # Port file stale or daemon not running
        PORT_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        return None


def is_daemon_running() -> bool:
    return get_daemon_port() is not None


def wait_for_daemon_port(timeout: float = 30.0, poll_interval: float = 0.1) -> int | None:
    """Wait for the daemon to publish a reachable port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = get_daemon_port()
        if port is not None:
            return port
        time.sleep(poll_interval)
    return None


def try_acquire_startup_lock() -> int | None:
    """Try to acquire the daemon startup lock, returning an fd on success."""
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(STARTUP_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def release_startup_lock(fd: int | None) -> None:
    """Release the daemon startup lock if held."""
    if fd is None:
        return
    try:
        os.close(fd)
    finally:
        STARTUP_LOCK_FILE.unlink(missing_ok=True)
