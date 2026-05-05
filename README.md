# tinymcp

Minimal [MCP](https://modelcontextprotocol.io/) server library for Python 3.13+.  
Stdlib only — no HTTP stack, no Pydantic, no external SDK.

## Install

As a path dependency in another `uv` project:

```toml
# pyproject.toml
dependencies = ["tinymcp"]

[tool.uv.sources]
tinymcp = { path = "../tinymcp", editable = true }
```

## Quick start

```python
from tinymcp import McpServer

mcp = McpServer("my-server")

@mcp.tool()
def greet(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}!"

mcp.run()   # reads stdin, writes stdout; blocks until EOF
```

Register the server in `pyproject.toml` as an MCP server and run it with `uv run`:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": ["run", "python", "-m", "myapp.mcp"]
    }
  }
}
```

## Tool return values

Tool functions may return:

- **`str`** — used as the text content verbatim (useful when the tool already produces JSON)
- **any JSON-serialisable object** — serialised with `json.dumps(result, default=str)`

## TCP bridge mode

Long-running processes (TUI, daemon) can start a local TCP server so that the
`mcp` subcommand bridges to it instead of starting its own MCP session:

**Long-running process (TUI side):**

```python
import asyncio, secrets
from tinymcp import serve_tcp

token = secrets.token_hex(32)
port_holder: list[int] = []

asyncio.create_task(
    serve_tcp(mcp, port=0, token=token, on_bound=port_holder.append)
)
# once port_holder[0] is set, write {"port": port_holder[0], "token": token}
# to a state file so the mcp subcommand can discover it
```

**`mcp` subcommand (stdio side):**

```python
import asyncio
from tinymcp import McpServer, run_mcp

state = read_state_file()   # your project reads its own state file
mcp = build_mcp_server()    # your McpServer with tools registered

asyncio.run(run_mcp(
    mcp,
    remote_port=state.port if state else None,
    remote_token=state.token if state else None,
    on_unreachable=delete_state_file,   # clean up stale state
))
```

`run_mcp` decision matrix:

| `remote_port` / `remote_token` | Port reachable? | Behaviour |
|---|---|---|
| Both provided | Yes | Bridge stdin/stdout to the TCP server |
| Both provided | No | Call `on_unreachable()` (if given), then run standalone |
| Either is `None` | — | Run standalone; `on_unreachable` is **not** called |

The state file format is project-specific; tinymcp only needs the `(port, token)` pair.

## API

### `McpServer(name)`

| Method | Description |
|--------|-------------|
| `tool(*, name=None, description=None, schema=None)` | Decorator — registers a callable as an MCP tool. Infers the JSON Schema from Python type hints. Optional kwargs override the auto-derived `name`, `description`, or the entire `schema` dict. |
| `run()` | Blocking stdio entry point (`asyncio.run` inside). |

Supported type hints for schema generation: `str`, `int`, `bool`, `float`, `None`, `list`, `list[T]`, `dict`, `dict[str, T]`, `X | Y`, `X | None` / `Optional[X]`, `Literal[...]`. Unknown hints fall back to `{"type": "string"}`.

### Transport helpers

| Function | Signature | Description |
|----------|-----------|-------------|
| `run_mcp` | `(server, *, remote_port=None, remote_token=None, on_unreachable=None)` | Standard `mcp` subcommand entry point. See decision matrix above. |
| `serve_tcp` | `(server, *, host, port, token, on_bound=None)` | Bind a TCP server; accept authenticated MCP connections. Runs until cancelled. `on_bound(actual_port)` fires once the socket is bound. |
| `run_stdio_bridge` | `(*, host, port, token)` | Connect to a running TCP server and forward stdin/stdout. |
| `run_stdio_standalone` | `(server)` | Run a full MCP session directly over stdin/stdout. |

### Constants

- `AUTH_TIMEOUT = 2.0` — seconds before unauthenticated connections are dropped
- `CONNECT_TIMEOUT = 0.5` — seconds used by `run_mcp` and `run_stdio_bridge` when probing/connecting
- `LOOPBACK_HOST = "127.0.0.1"` — default host for TCP operations

## Wire format

Newline-delimited JSON-RPC 2.0 over stdin/stdout or a loopback TCP socket.

**Auth frame** (TCP only) — first line the client sends before any MCP traffic:

```json
{"auth": "<token>"}
```

Connections that send the wrong token or time out during auth are closed silently.

## MCP methods supported

`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `prompts/list`, `resources/templates/list`.

Notifications (`notifications/initialized`, `notifications/cancelled`, `notifications/progress`) are accepted and silently discarded. Any message without an `id` field is treated as a notification (no response). Unknown methods with an `id` field receive a `-32601 Method not found` error.
