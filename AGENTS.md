# tinymcp — Agent Instructions

Stdlib-only MCP server library. No HTTP stack, no external SDK.  
Two modules, one invariant: every byte on the wire is newline-delimited JSON-RPC 2.0.

## Layout

| Path | Purpose |
|------|---------|
| `src/tinymcp/server.py` | `McpServer` class, `_Tool`, `_hint_to_schema`, `_build_input_schema` |
| `src/tinymcp/transport.py` | `serve_tcp`, `run_stdio_bridge`, `run_stdio_standalone`, `_serve_connection` |
| `src/tinymcp/__init__.py` | Re-exports public API |
| `tests/test_server.py` | Protocol-level tests — dispatch, schema, TCP auth, serve loop |

## Quality gates

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy --strict src/
uv run basedpyright src/
uv run python -m pytest tests/
```

All must pass before committing.

## Rules

1. **No runtime dependencies.** `server.py` and `transport.py` import only the Python stdlib. If a change requires a third-party package, stop and ask.
2. **No HTTP.** The library is deliberately HTTP-free. Do not add `httpx`, `aiohttp`, `starlette`, or any HTTP server/client.
3. **Wire format is fixed.** Messages are `{...json...}\n`. Auth frame is `{"auth": "<token>"}\n`. Do not change framing or auth format without updating both consumers (chronos, pony) in the same commit.
4. **`McpServer._serve` and `McpServer._handle` are the protocol core.** Keep them simple: one loop, one dispatch table. Do not add async I/O or threading inside them.
5. **Tool return convention.** `_handle` accepts tools that return either a `str` (used as text verbatim) or any JSON-serialisable object (serialised with `json.dumps`). Both must keep working.
6. **Tests cover the wire, not just the logic.** TCP auth tests must use real loopback sockets; use `asyncio.IsolatedAsyncioTestCase`. Do not mock the network.
7. **Schema generation is best-effort.** `_hint_to_schema` maps common Python hints to JSON Schema fragments. Unknown hints fall back to `{"type": "string"}` — this is intentional, not a bug.

## Key invariants

- `_EMPTY_LIST_RESPONSES` maps each empty-list method to its correct JSON key (`resources`, `prompts`, `resourceTemplates`). Do not use `method.split("/")[0] + "s"` — that produces wrong keys.
- `serve_tcp` calls `on_bound(actual_port)` exactly once, after the socket is bound but before `serve_forever()`. Consumers rely on this to write their state files.
- `AUTH_TIMEOUT` and `CONNECT_TIMEOUT` are public constants; consumers import them directly. Do not rename.

## `run_mcp` is the primary entry point

`run_mcp` owns the probe-and-decide logic shared by all consumers: probe
`remote_port`, bridge if reachable, fall back to standalone if not, call
`on_unreachable` on stale state.  Consumers read their own project-specific
state files and pass `(remote_port, remote_token)` in; they do not re-implement
the probe loop themselves.

Tests for `run_mcp` live in `RunMcpTest` in `tests/test_server.py`.  Consumer
tests (Chronos, Pony) only test the integration with their own state files.

## Adding a new MCP method

1. Add a branch to `McpServer._handle` in `server.py`.
2. Add a test in `tests/test_server.py` (dispatch + `_serve` round-trip).
3. Update `README.md`.

## Do NOT

- Add emojis, trailing-summary comments, or speculative abstractions.
- Introduce backwards-compatibility shims — there are only two consumers and they are co-located.
- Change `McpServer._serve` or `McpServer._handle` visibility to public without renaming (external callers already use the underscore names via `pyright: ignore`).
- Bump `requires-python` below 3.13 — consumers require 3.13.
