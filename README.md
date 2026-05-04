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

For long-running processes (e.g. a TUI) that want to expose MCP to a separate `mcp` subprocess without a second SQLite opener:

```python
import asyncio, secrets
from tinymcp import serve_tcp

token = secrets.token_hex(32)
port_holder: list[int] = []

async def main():
    mcp = build_mcp_server()   # your McpServer

    asyncio.create_task(
        serve_tcp(mcp, port=0, token=token, on_bound=port_holder.append)
    )
    # wait until port_holder[0] is set, write (port, token) to a state file,
    # then run the TUI.  The `mcp` subcommand reads the state file and calls
    # run_stdio_bridge() to proxy stdin/stdout to the TCP server.
```

```python
from tinymcp import run_stdio_bridge

await run_stdio_bridge(host="127.0.0.1", port=state.port, token=state.token)
```

## API

### `McpServer(name)`

| Method | Description |
|--------|-------------|
| `tool()` | Decorator — registers a callable as an MCP tool. Infers the JSON Schema from Python type hints. |
| `run()` | Blocking stdio entry point (`asyncio.run` inside). |

Supported type hints for schema generation: `str`, `bytes`, `int`, `bool`, `float`, `None`, `list`, `dict`, `X \| None` / `Optional[X]`. Unknown hints fall back to `{"type": "string"}`.

### Transport helpers

| Function | Description |
|----------|-------------|
| `serve_tcp(server, *, host, port, token, on_bound=None)` | Bind a TCP server; accept authenticated MCP connections. Runs until cancelled. `on_bound(actual_port)` is called once the socket is bound. |
| `run_stdio_bridge(*, host, port, token)` | Connect to a running TCP server and forward stdin/stdout. |
| `run_stdio_standalone(server)` | Run a full MCP session directly over stdin/stdout. |

### Constants

- `AUTH_TIMEOUT = 2.0` — seconds before unauthenticated connections are dropped
- `CONNECT_TIMEOUT = 0.5` — seconds before `run_stdio_bridge` gives up connecting

## Wire format

Newline-delimited JSON-RPC 2.0 over stdin/stdout or a loopback TCP socket.

**Auth frame** (TCP only) — first line the client sends before any MCP traffic:

```json
{"auth": "<token>"}
```

Connections that send the wrong token or time out during auth are closed silently.

## MCP methods supported

`initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `prompts/list`, `resources/templates/list`.

Notifications (`notifications/initialized`, `notifications/cancelled`, `notifications/progress`) are accepted and silently discarded. Unknown methods with an `id` field receive a `-32601 Method not found` error; unknown notifications receive no response.
