"""MCP transport helpers: TCP server, stdio bridge, stdio standalone.

Four async entry points:

``serve_tcp(server, *, host, port, token, on_bound)``
    Accepts MCP connections on *host*:*port*.  Each client must send a JSON
    auth frame ``{"auth": "<token>"}\\n`` as its first line; connections with
    the wrong token or that time out are closed silently.

``run_mcp(server, *, remote_port, remote_token, on_unreachable)``
    High-level entry point: probe *remote_port*, bridge if reachable, fall
    back to standalone if not.  Callers read their own state files and pass
    the discovered port + token; tinymcp owns the probe-and-decide logic.

``run_stdio_bridge(*, host, port, token)``
    Connects to a running TCP server and forwards stdin/stdout to it.

``run_stdio_standalone(server)``
    Runs an MCP session directly over stdin/stdout.

Message framing: newline-delimited JSON (one JSON-RPC object per line).
Auth frame: ``{"auth": "<token>"}\\n`` — sent by the client before any MCP
traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable

from tinymcp.server import McpServer

AUTH_TIMEOUT: float = 2.0
CONNECT_TIMEOUT: float = 0.5
LOOPBACK_HOST: str = "127.0.0.1"

logger = logging.getLogger(__name__)


async def serve_tcp(
    server: McpServer,
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
    token: str,
    on_bound: Callable[[int], None] | None = None,
) -> None:
    """Accept MCP connections on *host*:*port* until cancelled.

    If *on_bound* is provided it is called with the actual bound port
    (useful when *port*=0 and the OS assigns an ephemeral port).
    """

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await _serve_connection(reader, writer, server, token)
        except Exception:  # noqa: BLE001 — per-connection errors must not kill the server
            logger.exception("unhandled error in connection handler")
        finally:
            writer.close()
            await writer.wait_closed()

    tcp_server = await asyncio.start_server(_handle, host, port)
    actual_port: int = tcp_server.sockets[0].getsockname()[1]
    if on_bound is not None:
        on_bound(actual_port)
    async with tcp_server:
        await tcp_server.serve_forever()


async def run_stdio_bridge(*, host: str = LOOPBACK_HOST, port: int, token: str) -> None:
    """Forward stdin/stdout to the running TCP server.

    Sends the auth frame first, then pumps bytes in both directions
    until either end closes.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=CONNECT_TIMEOUT,
    )
    writer.write(json.dumps({"auth": token}).encode() + b"\n")
    await writer.drain()

    loop = asyncio.get_running_loop()

    async def stdin_to_tcp() -> None:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not line:
                break
            writer.write(line)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def tcp_to_stdout() -> None:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

    tasks = [
        asyncio.create_task(stdin_to_tcp()),
        asyncio.create_task(tcp_to_stdout()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_mcp(
    server: McpServer,
    *,
    remote_port: int | None = None,
    remote_token: str | None = None,
    on_unreachable: Callable[[], None] | None = None,
) -> None:
    """Run a full MCP session over stdin/stdout.

    If *remote_port* and *remote_token* are both given, probes the port.
    On success bridges stdin/stdout to the running TCP server.  If the port
    is not reachable, calls *on_unreachable* (if provided — e.g. to remove a
    stale state file) then falls back to a self-contained stdio session.

    This is the standard entry point for a ``mcp`` subcommand that supports
    both direct use and TUI bridge mode.  Callers read their own project-
    specific state files and pass the discovered port + token; this function
    owns the probe-and-decide logic shared by all consumers.
    """
    if remote_port is not None and remote_token is not None:
        try:
            _r, _w = await asyncio.wait_for(
                asyncio.open_connection(LOOPBACK_HOST, remote_port),
                timeout=CONNECT_TIMEOUT,
            )
            _w.close()
            await _w.wait_closed()
            await run_stdio_bridge(
                host=LOOPBACK_HOST, port=remote_port, token=remote_token
            )
            return
        except (TimeoutError, ConnectionRefusedError, OSError):
            if on_unreachable is not None:
                on_unreachable()
    await run_stdio_standalone(server)


async def run_stdio_standalone(server: McpServer) -> None:
    """Run an MCP session directly over stdin/stdout."""
    await server._run_stdio()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Internal connection handler
# ---------------------------------------------------------------------------


async def _serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    server: McpServer,
    token: str,
) -> None:
    """Authenticate one TCP connection then run an MCP session on it."""
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=AUTH_TIMEOUT)
        auth = json.loads(raw)
        if auth.get("auth") != token:
            return
    except (  # noqa: PERF203
        TimeoutError,
        json.JSONDecodeError,
        AttributeError,
        asyncio.LimitOverrunError,
    ):
        return

    async def readline() -> bytes:
        return await reader.readline()

    async def writeline(data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    await server._serve(readline, writeline)  # pyright: ignore[reportPrivateUsage]


__all__ = [
    "AUTH_TIMEOUT",
    "CONNECT_TIMEOUT",
    "LOOPBACK_HOST",
    "run_mcp",
    "run_stdio_bridge",
    "run_stdio_standalone",
    "serve_tcp",
]
