"""MCP transport helpers: TCP server, stdio bridge, stdio standalone.

Five async entry points:

``serve_tcp(server, *, host, port, token)``
    Accepts MCP connections on *host*:*port*.  Each client must send a JSON
    auth frame ``{"auth": "<token>"}\\n`` as its first line; connections with
    the wrong token or that time out are closed silently.  Returns
    ``(actual_port, task)``; cancel *task* to stop accepting.

``probe(host, port, *, timeout)``
    Return True if a TCP server is listening at *host*:*port*.

``run_mcp(server, *, remote, on_unreachable)``
    High-level entry point: probe *remote* (host, port, token), bridge if
    reachable, fall back to standalone if not.

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
import threading
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
) -> tuple[int, asyncio.Task[None]]:
    """Bind a TCP MCP server on *host*:*port*.

    Returns ``(actual_port, task)`` immediately after the socket is bound.
    The server is already accepting connections; cancel *task* to stop it
    and close the socket.
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

    async def _serve_forever() -> None:
        async with tcp_server:
            await tcp_server.serve_forever()

    return actual_port, asyncio.create_task(_serve_forever())


async def probe(host: str, port: int, *, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Return True if a TCP server is listening at *host*:*port*."""
    try:
        _r, _w = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        _w.close()
        await _w.wait_closed()
        return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


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
    stdin_buf = sys.stdin.buffer
    line_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def _stdin_reader() -> None:
        while True:
            data = stdin_buf.readline()
            loop.call_soon_threadsafe(line_queue.put_nowait, data)
            if not data:
                break

    threading.Thread(target=_stdin_reader, daemon=True).start()

    async def stdin_to_tcp() -> None:
        while True:
            line = await line_queue.get()
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
    remote: tuple[str, int, str] | None = None,
    on_unreachable: Callable[[], None] | None = None,
) -> None:
    """Run a full MCP session over stdin/stdout.

    If *remote* is ``(host, port, token)``, probes the port.  On success
    bridges stdin/stdout to the running TCP server.  If the port is not
    reachable, calls *on_unreachable* (if provided) then falls back to a
    self-contained stdio session.  When *remote* is ``None``, goes straight
    to standalone mode without calling *on_unreachable*.
    """
    if remote is not None:
        host, port, token = remote
        if await probe(host, port):
            await run_stdio_bridge(host=host, port=port, token=token)
            return
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
    "probe",
    "run_mcp",
    "run_stdio_bridge",
    "run_stdio_standalone",
    "serve_tcp",
]
