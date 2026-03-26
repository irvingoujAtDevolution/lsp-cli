"""Lightweight observability helpers for lsp-cli."""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lsp_cli.daemon_state import DAEMON_DIR

EVENTS_FILE = DAEMON_DIR / "events.jsonl"
_WRITE_LOCK = threading.Lock()


def emit_event(event: str, **fields: Any) -> None:
    """Append a structured event to the local JSONL trace file."""
    try:
        DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str)
        with _WRITE_LOCK:
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
    except Exception:
        # Observability must never break the tool.
        return


def read_events(limit: int = 50, event: str | None = None) -> list[dict[str, Any]]:
    """Read the most recent structured events."""
    if limit <= 0 or not EVENTS_FILE.exists():
        return []

    lines: deque[str] = deque(maxlen=limit * 4 if event else limit)
    with EVENTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            lines.append(line.rstrip("\n"))

    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event and item.get("event") != event:
            continue
        events.append(item)
        if len(events) >= limit:
            break

    events.reverse()
    return events
