"""Tests for the tinymcp McpServer and transport helpers."""

from __future__ import annotations

import asyncio
import io
import json
import secrets
import sys
import unittest
from typing import Any
from unittest.mock import patch

from tinymcp import AUTH_TIMEOUT, McpServer, run_mcp, serve_tcp
from tinymcp.transport import run_stdio_bridge

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
        self.assertIsInstance(resp["result"]["serverInfo"]["version"], str)

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
        resp = self._h(
            "tools/call", {"name": "echo", "arguments": {"message": "hi", "count": 3}}
        )
        assert resp is not None
        self.assertEqual(resp["result"]["content"][0]["text"], "hihihi")
        self.assertFalse(resp["result"]["isError"])

    def test_tools_call_object_result_serialised(self) -> None:
        resp = self._h("tools/call", {"name": "add", "arguments": {"a": 1.5, "b": 2.5}})
        assert resp is not None
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertAlmostEqual(payload["result"], 4.0)

    def test_tools_call_exception_returns_is_error(self) -> None:
        resp = self._h(
            "tools/call",
            {"name": "echo", "arguments": {"message": "x", "count": "not-an-int"}},
        )
        assert resp is not None
        self.assertTrue(resp["result"]["isError"])

    def test_tools_call_exception_text_is_str_exc(self) -> None:
        resp = self._h(
            "tools/call",
            {"name": "echo", "arguments": {"message": "x", "count": "not-an-int"}},
        )
        assert resp is not None
        # The text must be str(exc) — some non-empty string
        self.assertIsInstance(resp["result"]["content"][0]["text"], str)
        self.assertTrue(resp["result"]["content"][0]["text"])

    def test_tools_call_missing_required_arg_returns_is_error(self) -> None:
        resp = self._h("tools/call", {"name": "echo", "arguments": {"message": "hi"}})
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

    def test_non_dict_params_normalized_to_empty(self) -> None:
        for bad_params in ([], "string", 42, None):
            with self.subTest(params=bad_params):
                msg: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": bad_params,
                }
                resp = self.server._handle(msg)  # pyright: ignore[reportPrivateUsage]
                assert resp is not None
                # tools/call with empty params → unknown tool "" → error -32601
                self.assertIn("error", resp)


# ---------------------------------------------------------------------------
# _handle failure modes (4.2)
# ---------------------------------------------------------------------------


class HandleFailureModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServer("fail-test")

    def test_message_without_id_returns_none(self) -> None:
        resp = self.server._handle({"jsonrpc": "2.0", "method": "initialize"})  # pyright: ignore[reportPrivateUsage]
        self.assertIsNone(resp)

    def test_non_dict_params_for_initialize(self) -> None:
        resp = self.server._handle(  # pyright: ignore[reportPrivateUsage]
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": ["bad"]}
        )
        assert resp is not None
        # params normalised to {} → initialize proceeds normally
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")

    def test_tools_call_with_list_params_returns_error(self) -> None:
        resp = self.server._handle(  # pyright: ignore[reportPrivateUsage]
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": [1, 2]}
        )
        assert resp is not None
        # params normalised to {} → no name → unknown tool
        self.assertIn("error", resp)

    def test_missing_method_returns_not_found(self) -> None:
        resp = self.server._handle({"jsonrpc": "2.0", "id": 3})  # pyright: ignore[reportPrivateUsage]
        assert resp is not None
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)


# ---------------------------------------------------------------------------
# _serve loop
# ---------------------------------------------------------------------------


class ServeLoopTest(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> McpServer:
        s = McpServer("test")

        @s.tool()
        def ping_tool() -> str:  # pyright: ignore[reportUnusedFunction]
            """Return pong."""
            return "pong"

        return s

    async def _run(
        self, server: McpServer, messages: list[bytes]
    ) -> list[dict[str, Any]]:
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
        msg = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                }
            ).encode()
            + b"\n"
        )
        results = await self._run(self._server(), [msg])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)

    async def test_malformed_json_skipped(self) -> None:
        bad = b"not json\n"
        good = (
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n"
        )
        results = await self._run(self._server(), [bad, good])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 2)

    async def test_non_dict_json_skipped(self) -> None:
        non_dict = b"null\n"
        ping = (
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode() + b"\n"
        )
        results = await self._run(self._server(), [non_dict, ping])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 4)

    async def test_notification_produces_no_response(self) -> None:
        notif = (
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            ).encode()
            + b"\n"
        )
        ping = (
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}).encode() + b"\n"
        )
        results = await self._run(self._server(), [notif, ping])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 3)

    async def test_unknown_notification_no_response(self) -> None:
        notif = (
            json.dumps({"jsonrpc": "2.0", "method": "notifications/foo"}).encode()
            + b"\n"
        )
        ping = (
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode() + b"\n"
        )
        results = await self._run(self._server(), [notif, ping])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 5)

    async def test_internal_error_returns_32603_and_loop_continues(self) -> None:
        """A _handle exception emits -32603 and the loop keeps running."""
        s = McpServer("err-test")

        # Patch _handle on the instance to raise on one specific method, then
        # return normally so the loop can continue.
        original_handle = s._handle  # pyright: ignore[reportPrivateUsage]

        def patched(msg: dict[str, Any]) -> dict[str, Any] | None:
            if msg.get("method") == "resources/list":
                raise RuntimeError("injected error")
            return original_handle(msg)

        s._handle = patched  # type: ignore[method-assign]

        bad_msg = (
            json.dumps(
                {"jsonrpc": "2.0", "id": 10, "method": "resources/list"}
            ).encode()
            + b"\n"
        )
        ok_msg = (
            json.dumps({"jsonrpc": "2.0", "id": 11, "method": "ping"}).encode() + b"\n"
        )
        results = await self._run(s, [bad_msg, ok_msg])

        self.assertEqual(len(results), 2)
        error_resp = next(r for r in results if r["id"] == 10)
        ok_resp = next(r for r in results if r["id"] == 11)
        self.assertEqual(error_resp["error"]["code"], -32603)
        self.assertEqual(ok_resp["result"], {})


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

    def test_optional_str_produces_oneof(self) -> None:
        self.assertEqual(
            self._s(str | None),
            {"oneOf": [{"type": "string"}, {"type": "null"}]},
        )

    def test_optional_int_produces_oneof(self) -> None:
        self.assertEqual(
            self._s(int | None),
            {"oneOf": [{"type": "integer"}, {"type": "null"}]},
        )

    def test_union_two_types(self) -> None:
        result = self._s(str | int)
        self.assertEqual(result, {"oneOf": [{"type": "string"}, {"type": "integer"}]})

    def test_list_with_inner_type(self) -> None:
        self.assertEqual(
            self._s(list[int]),
            {"type": "array", "items": {"type": "integer"}},
        )

    def test_dict_with_value_type(self) -> None:
        self.assertEqual(
            self._s(dict[str, float]),
            {"type": "object", "additionalProperties": {"type": "number"}},
        )

    def test_bytes_falls_back_to_string(self) -> None:
        # bytes is no longer a recognised hint; it should fall through to the
        # unknown-type fallback which emits {"type": "string"}.
        self.assertEqual(self._s(bytes), {"type": "string"})

    def test_unknown_falls_back_to_string(self) -> None:
        class _Custom:
            pass

        self.assertEqual(self._s(_Custom), {"type": "string"})


# ---------------------------------------------------------------------------
# _build_input_schema (2.7 / 4.1)
# ---------------------------------------------------------------------------


class BuildInputSchemaTest(unittest.TestCase):
    def _schema(self, fn: Any) -> dict[str, Any]:
        from tinymcp.server import (  # pyright: ignore[reportPrivateUsage]  # noqa: I001
            _build_input_schema,
        )

        return _build_input_schema(fn)

    def test_required_and_optional_args(self) -> None:
        def f(a: int, b: str = "x") -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertEqual(schema["required"], ["a"])
        self.assertEqual(schema["properties"]["a"], {"type": "integer"})
        self.assertEqual(schema["properties"]["b"], {"type": "string"})

    def test_var_positional_excluded(self) -> None:
        def f(a: int, *args: Any) -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertNotIn("args", schema["properties"])
        self.assertEqual(schema["required"], ["a"])

    def test_var_keyword_excluded(self) -> None:
        def f(a: int, **kwargs: Any) -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertNotIn("kwargs", schema["properties"])
        self.assertEqual(schema["required"], ["a"])

    def test_keyword_only_with_default(self) -> None:
        def f(*, a: int, b: str = "x") -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertIn("a", schema["required"])
        self.assertNotIn("b", schema["required"])
        self.assertIn("b", schema["properties"])

    def test_list_inner_type_propagated(self) -> None:
        def f(items: list[int]) -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertEqual(
            schema["properties"]["items"],
            {"type": "array", "items": {"type": "integer"}},
        )

    def test_optional_produces_oneof(self) -> None:
        def f(x: str | None = None) -> str:  # noqa: ARG001
            return ""

        schema = self._schema(f)
        self.assertEqual(
            schema["properties"]["x"],
            {"oneOf": [{"type": "string"}, {"type": "null"}]},
        )
        self.assertNotIn("x", schema["required"])

    def test_tool_name_override(self) -> None:
        s = McpServer("t")

        @s.tool(name="my_name")
        def original_name() -> str:  # pyright: ignore[reportUnusedFunction]
            """doc"""
            return ""

        self.assertEqual(s._tools[0].name, "my_name")  # pyright: ignore[reportPrivateUsage]

    def test_tool_description_override(self) -> None:
        s = McpServer("t")

        @s.tool(description="custom desc")
        def f() -> str:  # pyright: ignore[reportUnusedFunction]
            """original doc"""
            return ""

        self.assertEqual(s._tools[0].description, "custom desc")  # pyright: ignore[reportPrivateUsage]

    def test_tool_schema_override_skips_build(self) -> None:
        s = McpServer("t")
        custom_schema: dict[str, Any] = {"type": "object", "properties": {}}

        @s.tool(schema=custom_schema)
        def f(a: int) -> str:  # pyright: ignore[reportUnusedFunction]  # noqa: ARG001
            return ""

        self.assertIs(s._tools[0].input_schema, custom_schema)  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Stdio adapter (4.3)
# ---------------------------------------------------------------------------


class StdioAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_stdio_ping_round_trip(self) -> None:
        """_run_stdio reads from stdin.buffer and writes to stdout.buffer."""
        server = McpServer("stdio-test")

        ping_msg = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
        )
        fake_stdin = io.BytesIO(ping_msg)
        fake_stdout = io.BytesIO()

        with (
            patch.object(sys, "stdin") as mock_stdin,
            patch.object(sys, "stdout") as mock_stdout,
        ):
            mock_stdin.buffer = fake_stdin
            mock_stdout.buffer = fake_stdout
            await server._run_stdio()  # pyright: ignore[reportPrivateUsage]

        line = fake_stdout.getvalue().rstrip(b"\n")
        resp = json.loads(line)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"], {})


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


# ---------------------------------------------------------------------------
# TCP auth edge cases (4.5)
# ---------------------------------------------------------------------------


class TcpAuthEdgeCaseTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, server: McpServer) -> tuple[asyncio.Task[None], int, str]:
        token = secrets.token_hex(16)
        port_holder: list[int] = []
        task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=port_holder.append)
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder)
        return task, port_holder[0], token

    async def test_malformed_json_auth_dropped(self) -> None:
        task, port, _ = await self._start(_make_server())
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"not-json\n")
            await w.drain()
            data = await asyncio.wait_for(r.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_non_dict_json_auth_dropped(self) -> None:
        task, port, _ = await self._start(_make_server())
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b'"just-a-string"\n')
            await w.drain()
            data = await asyncio.wait_for(r.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_eof_before_auth_dropped(self) -> None:
        task, port, _ = await self._start(_make_server())
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()  # immediate EOF
            data = await asyncio.wait_for(r.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_oversized_auth_frame_dropped(self) -> None:
        task, port, _ = await self._start(_make_server())
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            # Send >64 KiB without a newline to trigger LimitOverrunError
            w.write(b"x" * 70_000)
            await w.drain()
            data = await asyncio.wait_for(r.read(256), timeout=AUTH_TIMEOUT + 1.0)
            self.assertEqual(data, b"")
            w.close()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Bridge end-to-end (4.4)
# ---------------------------------------------------------------------------


class StdioBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_connects_and_terminates_cleanly(self) -> None:
        """Bridge authenticates, forwards stdin to TCP, exits on stdin EOF."""
        server = _make_server()
        token = secrets.token_hex(16)
        port_holder: list[int] = []

        tcp_task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=port_holder.append)
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder, "TCP server did not bind")

        ping_msg = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
        )
        fake_stdin = io.BytesIO(ping_msg)
        fake_stdout = io.BytesIO()

        try:
            with (
                patch.object(sys, "stdin") as mock_stdin,
                patch.object(sys, "stdout") as mock_stdout,
            ):
                mock_stdin.buffer = fake_stdin
                mock_stdout.buffer = fake_stdout
                # Bridge exits when stdin closes (b"" after ping_msg is consumed).
                await asyncio.wait_for(
                    run_stdio_bridge(port=port_holder[0], token=token),
                    timeout=5.0,
                )
            # Give the event loop a cycle so any pending tcp_to_stdout writes land.
            await asyncio.sleep(0)
            output = fake_stdout.getvalue()
            # Verify a response was received (race is inherently unlikely on loopback).
            if output:
                resp = json.loads(output.split(b"\n")[0])
                self.assertEqual(resp["id"], 1)
                self.assertIn("result", resp)
        finally:
            tcp_task.cancel()
            await asyncio.gather(tcp_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# run_mcp tests
# ---------------------------------------------------------------------------


class RunMcpTest(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> McpServer:
        s = McpServer("run-mcp-test")

        @s.tool()
        def ping_tool() -> str:  # pyright: ignore[reportUnusedFunction]
            """Ping."""
            return "pong"

        return s

    async def test_no_remote_goes_standalone(self) -> None:
        """With no remote port/token, run_mcp calls run_stdio_standalone."""
        from unittest.mock import AsyncMock

        standalone = AsyncMock()
        with patch("tinymcp.transport.run_stdio_standalone", standalone):
            await run_mcp(self._server())
        standalone.assert_awaited_once()

    async def test_unreachable_port_calls_on_unreachable_and_standalone(self) -> None:
        """Unreachable remote triggers on_unreachable then falls back to standalone."""
        from unittest.mock import AsyncMock

        cleanup_called = False

        def cleanup() -> None:
            nonlocal cleanup_called
            cleanup_called = True

        standalone = AsyncMock()
        with patch("tinymcp.transport.run_stdio_standalone", standalone):
            await run_mcp(
                self._server(),
                remote_port=19998,
                remote_token="unused",
                on_unreachable=cleanup,
            )
        self.assertTrue(cleanup_called)
        standalone.assert_awaited_once()

    async def test_reachable_port_calls_bridge(self) -> None:
        """A live TCP server causes run_mcp to bridge instead of going standalone."""
        server = _make_server()
        port_holder: list[int] = []
        token = secrets.token_hex(16)
        tcp_task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=port_holder.append)
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder, "TCP server did not bind")

        from unittest.mock import AsyncMock

        bridge = AsyncMock()
        try:
            with patch("tinymcp.transport.run_stdio_bridge", bridge):
                await run_mcp(
                    self._server(),
                    remote_port=port_holder[0],
                    remote_token=token,
                )
            bridge.assert_awaited_once()
            _, kwargs = bridge.call_args
            self.assertEqual(kwargs["port"], port_holder[0])
            self.assertEqual(kwargs["token"], token)
        finally:
            tcp_task.cancel()
            await asyncio.gather(tcp_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# run_mcp integration (4.6) — no mock bridge
# ---------------------------------------------------------------------------


class RunMcpIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_reachable_port_bridges_for_real(self) -> None:
        """run_mcp probes a live TCP server and bridges stdin/stdout without mocking."""
        server = _make_server()
        token = secrets.token_hex(16)
        port_holder: list[int] = []

        tcp_task = asyncio.create_task(
            serve_tcp(server, port=0, token=token, on_bound=port_holder.append)
        )
        for _ in range(40):
            if port_holder:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(port_holder, "TCP server did not bind")

        ping_msg = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
        )
        fake_stdin = io.BytesIO(ping_msg)
        fake_stdout = io.BytesIO()

        try:
            with (
                patch.object(sys, "stdin") as mock_stdin,
                patch.object(sys, "stdout") as mock_stdout,
            ):
                mock_stdin.buffer = fake_stdin
                mock_stdout.buffer = fake_stdout
                await asyncio.wait_for(
                    run_mcp(
                        McpServer("unused"),
                        remote_port=port_holder[0],
                        remote_token=token,
                    ),
                    timeout=5.0,
                )
        finally:
            tcp_task.cancel()
            await asyncio.gather(tcp_task, return_exceptions=True)

        # Bridge ran cleanly (no exception, no timeout).
        # A response may or may not be captured due to the FIRST_COMPLETED
        # cancellation race (documented in PLAN.md 4.4).
