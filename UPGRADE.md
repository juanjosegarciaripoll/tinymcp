# tinymcp — Upgrade Notes

This file documents every breaking change in `tinymcp` and the
mechanical recipe to update consuming code. Entries are listed in the
order they are expected to land (matching `PLAN.md` Phases 6–7). Each
section is self-contained: read the section that matches your current
tinymcp version → target version, apply the recipe to your consumer
repo, run its test suite.

Known consumers as of this writing: **chronos**, **pony**. Recipes
below assume these names; substitute as appropriate.

---

## §1 — `serve_tcp` no longer takes `on_bound`; returns the bound port

**Plan ref:** PLAN 6.1.

### Before

```python
from tinymcp import serve_tcp

async def main():
    def write_state(port: int) -> None:
        STATE_FILE.write_text(json.dumps({"port": port, "token": TOKEN}))

    await serve_tcp(server, port=0, token=TOKEN, on_bound=write_state)
```

### After

```python
from tinymcp import serve_tcp

async def main():
    port, serve_task = await serve_tcp(server, port=0, token=TOKEN)
    STATE_FILE.write_text(json.dumps({"port": port, "token": TOKEN}))
    await serve_task
```

`serve_tcp` now returns `(actual_port, asyncio.Task)` *as soon as the
listening socket is bound*, before accept-loop iterations begin. Callers
who used `on_bound` to write a state file should now write the file
between the `await serve_tcp(...)` line and `await serve_task`.

### Search-and-replace recipe

1. `grep -rn 'serve_tcp(' src/` in each consumer.
2. For each call site that passes `on_bound=`:
   - Lift the `on_bound` callback's body inline after the `serve_tcp`
     await.
   - Capture the tuple return: `port, task = await serve_tcp(...)`.
   - `await task` (or schedule it) at the appropriate place.
3. Remove the `on_bound` parameter from the call.
4. Run the consumer's tests; ensure the state file is still written
   *before* any client probes.

### Why

The callback shape forced an inversion of control; the natural Python
form is "give me the port, I'll write the file myself." It also made
testing harder (the test had to install a callback to learn the bound
port).

---

## §2 — `McpServer(name, version=...)` (informational; not a break)

**Plan ref:** PLAN 5.3 / 6.2.

`McpServer.__init__` now accepts an optional `version` kwarg. Existing
calls `McpServer(name)` continue to work unchanged. If you do not pass
`version`, the server reports the installed `tinymcp` package version
in `initialize.serverInfo.version`.

If your consumer was hard-asserting `serverInfo.version == "1.0"` in a
test, update that assertion (or stop asserting it — clients should not
pin server version).

### Recipe

```bash
grep -rn '"version".*"1\.0"' tests/   # find any pinned-version tests
```

Update each match to either match the new dynamic value or just check
the field is present and a string.

---

## §3 — Tool exceptions emit JSON-RPC `error`, not `isError: true`

**Plan ref:** PLAN 6.4. **Defer unless there's a concrete reason.**

### Before (old wire format)

A tool that raises produced:
```json
{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"<exc>"}],"isError":true}}
```

### After (new wire format)

A tool that raises produces:
```json
{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"<exc>"}}
```

### Consumer recipe

If your consumer treated tinymcp as a server only (i.e. you wrote tools
and let `tinymcp` deliver them to clients), no change is needed in your
code — clients see the new shape.

If your consumer also speaks the *client* side over the wire (e.g. tests
that read a tinymcp server's responses), update parsers:

```python
# Before
if response.get("result", {}).get("isError"):
    handle_tool_error(response["result"]["content"][0]["text"])

# After
if "error" in response and response["error"].get("code") == -32000:
    handle_tool_error(response["error"]["message"])
```

### Why

The split between "JSON-RPC error" (unknown tool) and "result with
isError" (tool exception) was an MCP-spec compromise. If/when the
consumers no longer need the MCP-spec shape, collapsing onto JSON-RPC
errors is simpler. **Only adopt if you have audited that no MCP client
relies on the `isError` shape.**

---

## §4 — `run_mcp` API: `remote=` tuple replaces `remote_port` / `remote_token`

**Plan ref:** PLAN 7.1.

### Before

```python
from tinymcp import run_mcp

state = read_state()  # {"port": ..., "token": ...} or None
run_mcp(
    server,
    remote_port=state["port"] if state else None,
    remote_token=state["token"] if state else None,
    on_unreachable=lambda: STATE_FILE.unlink(missing_ok=True),
)
```

### After

```python
from tinymcp import run_mcp, LOOPBACK_HOST

state = read_state()
remote = (LOOPBACK_HOST, state["port"], state["token"]) if state else None
run_mcp(
    server,
    remote=remote,
    on_unreachable=lambda: STATE_FILE.unlink(missing_ok=True),
)
```

A new public `probe(host, port, *, timeout=CONNECT_TIMEOUT) -> bool` is
also exported, for callers that want their own decision logic instead of
`run_mcp`'s built-in policy.

### Recipe

1. `grep -rn 'remote_port=\|remote_token=' src/` in each consumer.
2. For each call site, build a `(host, port, token)` tuple (or pass
   `remote=None`).
3. If you were doing your own probing previously, replace it with a
   `probe(...)` call.
4. Tests: any unit test that patched `tinymcp.transport.run_stdio_bridge`
   keeps working unchanged; tests that constructed a fake `remote_port`
   need to construct the tuple instead.

### Why

The two-kwarg shape conflated "remote location" with "remote auth," and
left no room for a non-loopback host. The tuple is more explicit and
exposes the host (currently always `127.0.0.1`).

---

## §5 — Stdio reader is no longer thread-blocked

**Plan ref:** PLAN 7.2.

### What changes for callers

- `McpServer.run()` and `run_stdio_standalone(server)` keep their
  signatures.
- `run_stdio_bridge` and `run_stdio_standalone` now cancel cleanly when
  their containing task is cancelled. Previously, a cancellation would
  leave a thread blocked on `sys.stdin.buffer.readline` until the next
  byte arrived.
- New optional kwargs on `McpServer.run(*, stdin=..., stdout=...)` and
  `run_stdio_bridge(*, stdin=..., stdout=..., ...)` accept any
  binary-mode file-like object. Default remains `sys.stdin.buffer` /
  `sys.stdout.buffer`.

### Consumer recipe

For most consumers: **no change required**. The defaults preserve
behaviour. The benefit is observable only in tests and supervised
shutdown paths.

If you previously worked around the thread leak by force-killing the
process, you can drop that workaround. If you ran `run_stdio_bridge`
inside an `asyncio.TaskGroup` and saw `RuntimeWarning: coroutine ...
was never awaited`, those warnings should disappear.

### Why

`loop.run_in_executor(None, stdin.readline)` cannot be cancelled cleanly
on any OS. The new implementation uses `selectors` on POSIX and a
managed pipe-pump thread on Windows so that cancellation is observable
within ~1 ms.

---

## Compatibility table

| tinymcp version | Breaking changes since previous  | Sections |
|-----------------|----------------------------------|----------|
| 0.x → 0.y       | `serve_tcp` returns the port     | §1       |
| 0.y → 0.z       | (optional) tool-error wire shape | §3       |
| 0.z → 0.w       | `run_mcp(remote=...)`            | §4       |
| 0.w → 0.v       | stdio cancellation               | §5       |

(Version numbers are placeholders — fill in at release time.)
