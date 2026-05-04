"""Tests for the tinymcp McpServer and transport helpers."""

from __future__ import annotations

import asyncio
import json
import secrets
import unittest
from typing import Any

from tinymcp import AUTH_TIMEOUT, McpServer, serve_tcp


# ---------------------------------------------------------------------------
# McpServer dispatch tests
# ---------------------------------------------------------------------------


class HandleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServer("test")

        @self.server.tool()
        def echo(  # pyright: ignore[reportUnusedFunction]
            message: str,
            count: int,
            flag: bool = False,  # noqa: ARG001
        ) -> str:
            """Echo message count times."""
            return message * count

        @self.server.tool()
        def add(  # pyright: ignore[reportUnusedFunction]
            a: float, b: float
        ) -> dict[str, Any]:
            """Return the sum as a JSON object."""
            return {"result": a + b}

    def _h(
        self, method: str, params: dict[str, Any] | None = None, msg_id: int = 1
    ) -> dict[str, Any] | None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.server._handle(msg)  # pyright: ignore[reportPrivateUsage]

    def test_initialize(self) -> None:
        resp = self._h(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        assert resp is not None
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "test")

    def test_ping(self) -> None:
        resp = self._h("ping")
        assert resp is not None
        self.assertEqual(resp["result"], {})

    def test_tools_list(self) -> None:
        resp = self._h("tools/list")
        assert resp is not None
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"echo", "add"})

    def test_tools_call_string_result(self) -> None:
        resp = self._h("tools/call", {"name": "echo", "arguments": {"message": "hi", "count": 3}})
        assert resp is not None
        self.assertEqual(resp["result"]["content"][0]["text"], "hihihi")
        self.assertFalse(resp["result"]["isError"])

    def test_tools_call_object_result_serialised(self) -> None:
        resp = self._h("tools/call", {"name": "add", "arguments": {"a": 1.5, "b": 2.5}})
        assert resp is not None
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertAlmostEqual(payload["result"], 4.0)

    def test_tools_call_exception_returns_is_error(self) -> None:
        resp = self._h("tools/call", {"name": "echo", "arguments": {"message": "x", "count": "not-an-int"}})
        assert resp is not None
        self.assertTrue(resp["result"]["isError"])

    def test_tools_call_unknown_tool(self) -> None:
        resp = self._h("tools/call", {"name": "no_such_tool", "arguments": {}})
        assert resp is not None
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_resources_list(self) -> None:
        resp = self._h("resources/list")
        assert resp is not None
        self.assertEqual(resp["result"]["resources"], [])

    def test_prompts_list(self) -> None:
        resp = self._h("prompts/list")
        assert resp is not None
        self.assertEqual(resp["result"]["prompts"], [])

    def test_resources_templates_list(self) -> None:
        resp = self._h("resources/templates/list")
        assert resp is not None
        self.assertEqual(resp["result"]["resourceTemplates"], [])

    def test_unknown_method_with_id(self) -> None:
        resp = self._h("unknown/method")
        assert resp is not None
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_notification_returns_none(self) -> None:
        resp = self.server._handle({"jsonrpc": "2.0", "method": "unknown/event"})  # pyright: ignore[reportPrivateUsage]
        self.assertIsNone(resp)

    def test_known_notifications_return_none(self) -> None:
        for method in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
        ):
            with self.subTest(method=method):
                self.assertIsNone(
                    self.server._handle({"jsonrpc": "2.0", "method": method})  # pyright: ignore[reportPrivateUsage]
                )


class ServeLoopTest(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> McpServer:
        s = McpServer("test")

        @s.tool()
        def ping_tool() -> str:  # pyright: ignore[reportUnusedFunction]
            """Return pong."""
            return "pong"

        return s

    async def _run(self, server: McpServer, messages: list[bytes]) -> list[dict[str, Any]]:
        """Feed *messages* into the server, collect all responses, stop on EOF."""
        req: asyncio.Queue[bytes] = asyncio.Queue()
        resp: asyncio.Queue[bytes] = asyncio.Queue()

        for m in messages:
            await req.put(m)
        await req.put(b"")  # EOF

        task = asyncio.create_task(
            server._serve(req.get, resp.put)  # pyright: ignore[reportPrivateUsage]
        )
        await task
        results: list[dict[str, Any]] = []
        while not resp.empty():
            results.append(json.loads(await resp.get()))
        return results

    async def test_initialize_response(self) -> None:
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}}}).encode() + b"\n"
        results = await self._run(self._server(), [msg])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)

    async def test_malformed_json_skipped(self) -> None:
        bad = b"not json\n"
        good = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n"
        results = await self._run(self._server(), [bad, good])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 2)

    async def test_notification_produces_no_response(self) -> None:
        notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"
        ping = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}).encode() + b"\n"
        results = await self._run(self._server(), [notif, ping])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 3)


# ---------------------------------------------------------------------------
# _hint_to_schema
# ---------------------------------------------------------------------------


class HintToSchemaTest(unittest.TestCase):
    def _s(self, hint: Any) -> dict[str, Any]:
        from tinymcp.server import _hint_to_schema  # pyright: ignore[reportPrivateUsage]  # noqa: I001

        return _hint_to_schema(hint)

    def test_str(self) -> None:
        self.assertEqual(self._s(str), {"type": "string"})

    def test_int(self) -> None:
        self.assertEqual(self._s(int), {"type": "integer"})

    def test_bool(self) -> None:
        self.assertEqual(self._s(bool), {"type": "boolean"})

    def test_float(self) -> None:
        self.assertEqual(self._s(float), {"type": "number"})

    def test_none_type(self) -> None:
        self.assertEqual(self._s(type(None)), {"type": "null"})

    def test_list(self) -> None:
        self.assertEqual(self._s(list), {"type": "array"})

    def test_dict(self) -> None:
        self.assertEqual(self._s(dict), {"type": "object"})

    def test_optional_str(self) -> None:
        self.assertEqual(self._s(str | None), {"type": "string"})

    def test_unknown_falls_back_to_string(self) -> None:
        class _Custom:
            pass

        self.assertEqual(self._s(_Custom), {"type": "string"})


# ---------------------------------------------------------------------------
# TCP transport tests
# ---------------------------------------------------------------------------


def _make_server() -> McpServer:
    s = McpServer("tcp-test")

    @s.tool()
    def greet(name: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Greet someone."""
        return f"Hello, {name}!"

    return s


class TcpAuthTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, server: McpServer) -> tuple[asyncio.Task[None], int]:
        token = secrets.token_hex(16)
        port_holder: list[int] = []

        def on_bound(p: int) -> None:
            port_holder.append(p)

        task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=on_bound)
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder, "server did not bind in time")
        return task, port_holder[0], token  # type: ignore[return-value]

    async def test_wrong_token_closes_connection(self) -> None:
        server = _make_server()
        task, port, token = await self._start(server)  # type: ignore[assignment]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(json.dumps({"auth": "WRONG"}).encode() + b"\n")
            await w.drain()
            data = await asyncio.wait_for(r.read(256), timeout=1.0)
            self.assertEqual(data, b"")
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_auth_times_out(self) -> None:
        server = _make_server()
        task, port, token = await self._start(server)  # type: ignore[assignment]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            data = await asyncio.wait_for(r.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_correct_token_gets_initialize_response(self) -> None:
        server = _make_server()
        task, port, token = await self._start(server)  # type: ignore[assignment]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(json.dumps({"auth": token}).encode() + b"\n")
            await w.drain()
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
            w.write(json.dumps(init).encode() + b"\n")
            await w.drain()
            line = await asyncio.wait_for(r.readline(), timeout=2.0)
            resp = json.loads(line)
            self.assertEqual(resp["id"], 1)
            self.assertIn("result", resp)
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
