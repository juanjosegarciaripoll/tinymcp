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
import importlib.metadata
import inspect
import json
import logging
import sys
import types as _types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

_MCP_PROTOCOL_VERSION = "2024-11-05"
_F = TypeVar("_F", bound=Callable[..., Any])

_EMPTY_LIST_RESPONSES: dict[str, str] = {
    "resources/list": "resources",
    "prompts/list": "prompts",
    "resources/templates/list": "resourceTemplates",
}

logger = logging.getLogger(__name__)

def _get_server_version() -> str:
    try:
        return importlib.metadata.version("tinymcp")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


_SERVER_VERSION: str = _get_server_version()


# ---------------------------------------------------------------------------
# JSON-RPC response helpers
# ---------------------------------------------------------------------------


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


@dataclass
class _Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------


def _hint_to_schema(hint: Any) -> dict[str, Any]:
    """Best-effort Python type hint → JSON Schema fragment."""
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    # Union types: Optional[X], X | Y, Union[X, Y, ...]
    if origin is typing.Union or isinstance(hint, _types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        has_none = type(None) in args
        branches = [_hint_to_schema(a) for a in non_none]
        if has_none:
            branches.append({"type": "null"})
        if not branches:
            return {"type": "null"}
        if len(branches) == 1:
            return branches[0]
        return {"oneOf": branches}

    if hint is str:
        return {"type": "string"}
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is type(None):
        return {"type": "null"}
    if origin is list or hint is list:
        if args:
            return {"type": "array", "items": _hint_to_schema(args[0])}
        return {"type": "array"}
    if origin is dict or hint is dict:
        if args and len(args) >= 2:
            return {"type": "object", "additionalProperties": _hint_to_schema(args[1])}
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
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        properties[name] = _hint_to_schema(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class McpServer:
    """Minimal MCP server: register tools, serve over stdio or a stream pair."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._tools: list[_Tool] = []

    def tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        schema: dict[str, Any] | None = None,
    ) -> Callable[[_F], _F]:
        """Decorator: register a callable as an MCP tool.

        Optional keyword arguments override the auto-derived metadata:
        ``name``, ``description``, and ``schema`` (the full inputSchema dict).
        When ``schema`` is provided ``_build_input_schema`` is not called.
        """

        def decorator(fn: _F) -> _F:
            self._tools.append(
                _Tool(
                    name=name if name is not None else fn.__name__,
                    description=description
                    if description is not None
                    else (fn.__doc__ or "").strip(),
                    input_schema=schema
                    if schema is not None
                    else _build_input_schema(fn),
                    fn=fn,
                )
            )
            return fn

        return decorator

    def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC 2.0 message; return a response or None."""
        method: str = msg.get("method", "")
        msg_id = msg.get("id")

        # Any message without an id is a notification — no response.
        if msg_id is None:
            return None

        raw_params = msg.get("params")
        params: dict[str, Any] = (
            cast(dict[str, Any], raw_params) if isinstance(raw_params, dict) else {}
        )

        if method == "initialize":
            return _ok(
                msg_id,
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self._name, "version": _SERVER_VERSION},
                },
            )

        if method == "ping":
            return _ok(msg_id, {})

        if method == "tools/list":
            return _ok(
                msg_id,
                {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                        }
                        for t in self._tools
                    ]
                },
            )

        if method == "tools/call":
            name = params.get("name", "")
            arguments: dict[str, Any] = params.get("arguments") or {}
            tool = next((t for t in self._tools if t.name == name), None)
            if tool is None:
                return _err(msg_id, -32601, f"Unknown tool: {name}")
            try:
                result = tool.fn(**arguments)
                text = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, default=str)
                )
                return _ok(
                    msg_id,
                    {"content": [{"type": "text", "text": text}], "isError": False},
                )
            except Exception as exc:
                return _ok(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )

        if method in _EMPTY_LIST_RESPONSES:
            return _ok(msg_id, {_EMPTY_LIST_RESPONSES[method]: []})

        return _err(msg_id, -32601, f"Method not found: {method}")

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
                msg: Any = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            msg_dict = cast(dict[str, Any], msg)
            try:
                response = self._handle(msg_dict)
            except Exception:
                logger.exception("unhandled error in _handle")
                recovered_id: Any = msg_dict.get("id")
                response = (
                    _err(recovered_id, -32603, "Internal error")
                    if recovered_id is not None
                    else None
                )
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
