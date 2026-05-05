# tinymcp — Improvement Plan

A fine-grained, ordered task list. Tasks are grouped into **phases**.
Phases 1–5 are **non-breaking** for current consumers (chronos, pony) —
they change internal correctness, robustness, and tests without altering
the public Python API or the wire format in ways that break a working
client. Phases 6–7 are **breaking**; each item has a corresponding entry
in `UPGRADE.md`.

Quality gates after every task:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy --strict src/
uv run basedpyright src/
uv run python -m pytest tests/
```

Commit per task (or per tightly-coupled pair) so each change is
revertable.

---

## Phase 1 — Error containment (no behaviour change on happy paths)

- [x] **1.1** In `server.py::McpServer._serve`, wrap the `self._handle(msg)`
      call in `try/except Exception`. On exception:
  - if the offending message had an `id` → emit
    `{"jsonrpc": "2.0", "id": <id>, "error": {"code": -32603, "message": "Internal error"}}`;
  - else (notification or no id) → log via
    `logging.getLogger("tinymcp").exception(...)` and continue.
  - Add a test in `ServeLoopTest` that registers a tool that raises *before*
    the `tools/call` try/except can catch it (e.g. by sending
    `{"method": "tools/call", "params": "not-a-dict", "id": 7}`) and
    asserts (a) the loop survives a follow-up `ping`, (b) the response is
    `-32603`.

- [x] **1.2** In `server.py::McpServer._handle`, normalise non-dict
      `params` to `{}`:
  ```python
  raw = msg.get("params")
  params = raw if isinstance(raw, dict) else {}
  ```
  Test: send `params` as a list, a string, and `null`; assert no crash and
  sane responses for `initialize`, `tools/list`, `tools/call`.

- [x] **1.3** In `server.py::McpServer._handle`, handle the case where the
      top-level message is not a dict (e.g. JSON `null`, JSON list). Emit
      `-32600 Invalid Request` if an `id` can be recovered (it usually
      can't), else swallow. Test in `ServeLoopTest`.

- [x] **1.4** Extract two small helpers in `server.py`:
  ```python
  def _ok(msg_id, result): return {"jsonrpc":"2.0","id":msg_id,"result":result}
  def _err(msg_id, code, message):
      return {"jsonrpc":"2.0","id":msg_id,"error":{"code":code,"message":message}}
  ```
  Replace inline `{"jsonrpc": "2.0", ...}` literals in `_handle` with calls
  to these helpers. Pure refactor, no wire change.

- [x] **1.5** Replace the `if/elif method == "..."` chain in `_handle` with
      a method-name dispatch table mapping to bound methods/closures.
      `notifications/*` keep their early-return path. Pure refactor.

- [x] **1.6** Drop the explicit `notifications/...` allow-list — rely on
      the existing `msg_id is None` check at the response stage. Spec:
      any message without `id` is a notification. Test that an unknown
      `notifications/foo` produces no response and does not log a warning.

## Phase 2 — Schema generation correctness

These change the JSON Schema in `tools/list` to be *more correct*; any
client that relied on the old (incorrect) schema was already broken.

- [x] **2.1** `_hint_to_schema(Optional[X])` → emit
      `{"type": ["<x_type>", "null"]}` (or `{"oneOf": [...]}` if `X` is
      compound). Add a direct unit test in `HintToSchemaTest`.

- [x] **2.2** `_hint_to_schema(list[T])` / `dict[str, T]` → populate
      `items` / `additionalProperties` from `_hint_to_schema(T)`. Bare
      `list` / `dict` keep their current schemas. Test nested generics.

- [x] **2.3** `_hint_to_schema(A | B)` (non-Optional union) → `{"oneOf":
      [_hint_to_schema(A), _hint_to_schema(B)]}`. Test.

- [x] **2.4** `_build_input_schema` skips parameters whose `kind` is
      `VAR_POSITIONAL` or `VAR_KEYWORD`. Test by registering a tool
      `def f(a: int, *args, **kwargs) -> str: ...` and asserting only `a`
      appears in `properties` and `required`.

- [x] **2.5** Drop `bytes` from the `_hint_to_schema` mapping and from
      `README.md`'s "supported hints" list. A `bytes` parameter cannot
      survive JSON-RPC; the current `{"type": "string"}` mapping is
      misleading. (If you'd rather keep it: map to
      `{"type": "string", "contentEncoding": "base64"}` and decode on
      receipt — flag if you want this branch instead.)

- [ ] **2.6** *(Optional, additive.)* `_hint_to_schema(Literal[...])` →
      `{"enum": [...]}`. Test.

- [x] **2.7** Add a direct test class `BuildInputSchemaTest` covering:
      required-args computation, default handling, mixed
      keyword-only/positional, the post-2.4 var-args exclusion, the
      post-2.1/2.2/2.3 generics.

## Phase 3 — Transport robustness

- [x] **3.1** Add `logger = logging.getLogger(__name__)` at module top of
      `transport.py`. Replace `except Exception: pass` in
      `serve_tcp`'s connection handler and in `_serve_connection`'s
      auth-failure path with `logger.exception(...)` (still swallowed —
      no behaviour change beyond visibility). Default log level keeps it
      silent for callers who don't configure logging.

- [x] **3.2** After every `writer.close()` in `transport.py`, also
      `await writer.wait_closed()`. Affected sites: `serve_tcp`'s
      connection-handler `finally`, `_serve_connection`'s `finally`,
      `run_mcp`'s probe block.

- [x] **3.3** Bound the auth-frame read in `_serve_connection`. Replace
      the unbounded `await reader.readline()` with
      `await reader.readuntil(b"\n")` inside `try/except
      asyncio.LimitOverrunError`, treating overflow as auth failure. The
      default `StreamReader` limit (~64 KiB) is plenty for `{"auth":
      "<token>"}\n`. Add a test that sends a 1 MiB frame without a
      newline and asserts the connection is dropped within
      `AUTH_TIMEOUT`.

- [x] **3.4** Add a module-level constant
      `LOOPBACK_HOST = "127.0.0.1"` and use it as the default in
      `serve_tcp(host=...)`, in `run_stdio_bridge`'s connection target,
      and in `run_mcp`'s probe. Pure refactor.

- [ ] **3.5** *(Optional.)* Wrap `writer.drain()` with
      `asyncio.wait_for(..., timeout=DRAIN_TIMEOUT)` and define
      `DRAIN_TIMEOUT = 5.0` as a module-level public constant. On timeout,
      close the connection. Add a test using a TCP server that never
      reads.

## Phase 4 — Test coverage gaps

- [x] **4.1** `BuildInputSchemaTest` (already covered by 2.7).

- [x] **4.2** `_handle` failure modes test class: non-dict `params`,
      list/null top-level message, missing `method`, `tools/call` with
      missing required args (TypeError → `isError: true`), tool exception
      message text matches `str(exc)`.

- [x] **4.3** Stdio adapter (`McpServer._run_stdio`) test. Approach:
      patch `sys.stdin`/`sys.stdout` with `io.BytesIO` wrapped in
      `BufferedReader`/`BufferedWriter`, call `asyncio.run(server._run_stdio())`
      after closing stdin to force EOF, assert one `initialize` round-trip
      lands on stdout.

- [x] **4.4** End-to-end `run_stdio_bridge` test. Use real `serve_tcp` on
      port 0 with `on_bound`, real `run_stdio_bridge` with patched
      stdin/stdout pipes, assert an `initialize` round-trip. This
      exercises the dual-pump path that today is only mock-tested.
      Document the known stdin-thread leak as a `# noqa` comment in the
      test if it surfaces.

- [x] **4.5** TCP auth edge cases (added in `TcpAuthTest`):
      auth frame that is malformed JSON, auth frame that parses to a
      non-dict, oversized auth frame (drives 3.3), EOF before any auth
      frame.

- [x] **4.6** `RunMcpTest` integration variant that does not mock
      `run_stdio_bridge` — runs both halves over loopback. Keep the
      existing mock-based test as a fast unit test.

- [ ] **4.7** *(Optional.)* `pytest.importorskip("jsonschema")` test that
      validates each emitted `tools/list` `inputSchema` against
      Draft-2020-12. Skipped automatically when `jsonschema` is not
      installed; never imported at runtime.

## Phase 5 — Tooling, docs, additive niceties

- [x] **5.1** Remove `[tool.hatch.build.targets.wheel]` from
      `pyproject.toml`. The build backend is `uv_build`; the hatch block
      is dead config.

- [x] **5.2** Remove `pytest-asyncio` from dev deps and the
      `asyncio_mode = "auto"` setting. Tests use
      `unittest.IsolatedAsyncioTestCase`. Re-run all gates.

- [x] **5.3** Read `serverInfo.version` in
      `McpServer._handle("initialize")` from
      `importlib.metadata.version("tinymcp")` with a static
      `"0.0.0+unknown"` fallback. Pure additive change to the
      `initialize` response (clients should not pin server version).

- [x] **5.4** Update `README.md`:
  - drop `bytes` from the supported-hints list (or document base64
    behaviour, matching 2.5);
  - clarify `run_mcp`'s decision matrix (probe success → bridge; probe
    failure → standalone + `on_unreachable`; missing `remote_port` /
    `remote_token` → standalone, `on_unreachable` *not* called);
  - remove the "avoiding a competing SQLite opener" comment that leaks a
    consumer-specific detail.

- [x] **5.5** *(Additive, non-breaking.)* `McpServer.tool()` accepts
      optional kwargs `name=`, `description=`, `schema=` to override the
      auto-derived metadata. When `schema` is provided, `_build_input_schema`
      is not called. Existing call sites (`@mcp.tool()`) are unaffected.
      Add tests for each kwarg.

## Phase 6 — Breaking changes (need explicit OK + UPGRADE.md follow-through)

Each item below has a matching section in `UPGRADE.md`. Land at most one
breaking change per release; bump the package minor version each time and
publish UPGRADE.md notes.

- [ ] **6.1** Replace `serve_tcp(..., on_bound=callback)` with
      `serve_tcp(...)` returning `(actual_port, server_task)` (or expose
      a `started: asyncio.Event` + `port` attribute). Removes the
      callback shape. **UPGRADE.md §1.**

- [ ] **6.2** Add `version: str` parameter to `McpServer.__init__` —
      *strictly additive*; not a break in itself, but lands here together
      with 6.1 if you want both in one minor bump. Skip from this phase
      if 5.3 (read from package metadata) is enough. **UPGRADE.md §2 (informational).**

- [ ] **6.3** Add `McpServer.run(*, transport=..., stdin=..., stdout=...)`
      so callers can plug in custom streams. Today `McpServer.run()`
      takes no arguments and is hard-wired to `sys.stdin` / `sys.stdout`.
      Existing zero-arg callers stay valid (defaults match today's
      behaviour) — *not* a break. List here only if you want the new
      kwargs covered by tests as part of the same release; otherwise keep
      it in Phase 5 as additive.

- [ ] **6.4** Switch tool-exception convention from
      `{"isError": true, "content": [...]}` to JSON-RPC
      `error: {"code": -32000, ...}`. Wire-format break that touches both
      consumers. **UPGRADE.md §3.** Recommend deferring unless there's a
      concrete reason.

## Phase 7 — Breaking changes around `run_mcp` / stdio cancellation

- [ ] **7.1** Split `run_mcp` into:
  - `probe(host, port, *, timeout=CONNECT_TIMEOUT) -> bool` (public),
  - `run_mcp(server, *, remote=None, on_unreachable=None)` where `remote`
    is `(host, port, token) | None`.
  Removes the `remote_port` / `remote_token` kwargs. **UPGRADE.md §4.**

- [ ] **7.2** Replace `loop.run_in_executor(None, sys.stdin.buffer.readline)`
      in `_run_stdio` and `run_stdio_bridge` with a non-blocking stdin
      reader (selectors-based on POSIX, `msvcrt` or a managed thread with
      a sentinel on Windows). Required for clean cancellation; today the
      blocking-readline thread leaks. May require a new
      `run_stdio(*, stdin=, stdout=)` parameter — overlaps with 6.3.
      **UPGRADE.md §5.**

---

## Suggested release rhythm

- Phases 1–5: one PR per phase, all in a single `0.x` minor bump (no
  consumer changes needed).
- Phase 6: each task its own PR + minor bump + UPGRADE.md section + a
  PR in each consumer (chronos, pony) that follows the UPGRADE recipe.
- Phase 7: same cadence as Phase 6.
