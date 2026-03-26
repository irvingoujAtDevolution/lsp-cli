"""JSON-RPC protocol for CLI <-> daemon communication over named pipe/socket."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any


@dataclass
class Request:
    method: str
    params: dict[str, Any]
    id: int

    def to_bytes(self) -> bytes:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": self.method,
            "params": self.params,
            "id": self.id,
        }).encode("utf-8")
        return struct.pack("!I", len(payload)) + payload


@dataclass
class Response:
    id: int
    result: Any = None
    error: dict[str, Any] | None = None

    def to_bytes(self) -> bytes:
        obj: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            obj["error"] = self.error
        else:
            obj["result"] = self.result
        payload = json.dumps(obj).encode("utf-8")
        return struct.pack("!I", len(payload)) + payload


MAX_MESSAGE_SIZE = 16 * 1024 * 1024  # 16 MB


def read_message(data: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Read a length-prefixed JSON message from a buffer.

    Returns (message_dict, remaining_bytes) or (None, data) if incomplete.
    Raises ValueError if message exceeds MAX_MESSAGE_SIZE.
    """
    if len(data) < 4:
        return None, data
    length = struct.unpack("!I", data[:4])[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    if len(data) < 4 + length:
        return None, data
    payload = data[4:4 + length]
    remaining = data[4 + length:]
    return json.loads(payload.decode("utf-8")), remaining


def make_error(id: int, code: int, message: str) -> Response:
    return Response(id=id, error={"code": code, "message": message})
