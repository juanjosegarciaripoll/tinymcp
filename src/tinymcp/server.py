"""Minimal MCP server — no external SDK required.

Implements the MCP stdio wire format (newline-delimited JSON-RPC 2.0)
directly using only the Python standard library.

Usage::

    from tinymcp import McpServer

    mcp = McpServer("my-server")

    @mcp.tool()
    def greet(name: str) -> str:
        \"\"\"Say hello.\"\"\"
        return f"Hello, {name}!"

    mcp.run()          # blocking stdio mode
    # or: await mcp._run_stdio()   inside an existing event loop
    # or: pass to tinymcp.serve_tcp / run_stdio_standalone

Tool functions may return a ``str`` (used as-is) or any JSON-serialisable
Python object (serialised with ``json.dumps``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

_MCP_PROTOCOL_VERSION = "2024-11-05"
_F = TypeVar("_F", bound=Callable[..., Any])

_EMPTY_LIST_RESPONSES: dict[str, str] = {
    "resources/list": "resources",
    "prompts/list": "prompts",
    "resources/templates/list": "resourceTemplates",
}


@dataclass
class _Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]


def _hint_to_schema(hint: Any) -> dict[str, Any]:
    """Best-effort Python type hint → JSON Schema fragment."""
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is not None and args and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _hint_to_schema(non_none[0])
    if hint is str or hint is bytes:
        return {"type": "string"}
    if hint is int:
        return {"type": "integer"}
    if hint is bool:
        return {"type": "boolean"}
    if hint is float:
        return {"type": "number"}
    if hint is type(None):
        return {"type": "null"}
    if origin is list or hint is list:
        return {"type": "array"}
    if origin is dict or hint is dict:
        return {"type": "object"}
    return {"type": "string"}


def _build_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        properties[name] = _hint_to_schema(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


class McpServer:
    """Minimal MCP server: register tools, serve over stdio or a stream pair."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._tools: list[_Tool] = []

    def tool(self) -> Callable[[_F], _F]:
        """Decorator: register a callable as an MCP tool."""

        def decorator(fn: _F) -> _F:
            self._tools.append(
                _Tool(
                    name=fn.__name__,
                    description=(fn.__doc__ or "").strip(),
                    input_schema=_build_input_schema(fn),
                    fn=fn,
                )
            )
            return fn

        return decorator

    def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC 2.0 message; return a response or None."""
        method: str = msg.get("method", "")
        msg_id = msg.get("id")
        params: dict[str, Any] = msg.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self._name, "version": "1.0"},
                },
            }

        if method in {
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
        }:
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                        }
                        for t in self._tools
                    ]
                },
            }

        if method == "tools/call":
            name = params.get("name", "")
            arguments: dict[str, Any] = params.get("arguments") or {}
            tool = next((t for t in self._tools if t.name == name), None)
            if tool is None:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            try:
                result = tool.fn(**arguments)
                text = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, default=str)
                )
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }

        if method in _EMPTY_LIST_RESPONSES:
            key = _EMPTY_LIST_RESPONSES[method]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}

        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return None

    async def _serve(
        self,
        readline: Callable[[], Any],
        writeline: Callable[[bytes], Any],
    ) -> None:
        """Read/dispatch/write loop shared by stdio and TCP handlers."""
        while True:
            raw = await readline()
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            response = self._handle(msg)
            if response is not None:
                line = json.dumps(response, separators=(",", ":")).encode() + b"\n"
                await writeline(line)

    def run(self) -> None:
        """Run the MCP server over stdin/stdout (blocking)."""
        asyncio.run(self._run_stdio())

    async def _run_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        stdin_buf = sys.stdin.buffer
        stdout_buf = sys.stdout.buffer

        async def readline() -> bytes:
            return await loop.run_in_executor(None, stdin_buf.readline)

        async def writeline(data: bytes) -> None:
            stdout_buf.write(data)
            stdout_buf.flush()

        await self._serve(readline, writeline)


__all__ = ["McpServer"]
