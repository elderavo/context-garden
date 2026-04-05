"""JSON-RPC protocol — request/response parsing for stdio transport."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class RpcRequest:
    """Inbound JSON-RPC request."""

    id: str
    method: str
    params: dict[str, Any]


@dataclass
class RpcResponse:
    """Outbound JSON-RPC response (success)."""

    id: str
    result: dict[str, Any]


@dataclass
class RpcError:
    """Outbound JSON-RPC error response."""

    id: str
    code: int
    message: str


def parse_request(line: str) -> RpcRequest:
    """Parse a newline-delimited JSON request."""
    data = json.loads(line)
    return RpcRequest(
        id=str(data["id"]),
        method=str(data["method"]),
        params=data.get("params", {}),
    )


def send_response(resp: RpcResponse | RpcError) -> None:
    """Write a newline-delimited JSON response to stdout."""
    if isinstance(resp, RpcError):
        payload = {"id": resp.id, "error": {"code": resp.code, "message": resp.message}}
    else:
        payload = {"id": resp.id, "result": resp.result}
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def send_notification(payload: dict) -> None:
    """Write a newline-delimited JSON notification to stdout (no id — not a response)."""
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
