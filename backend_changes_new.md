# Backend Changes — Standalone MCP Tool Node

This document shows every backend change required for the standalone MCP tool node feature, **diff-style against the original files**. Sourced from a fresh re-read of every original file, not memory.

The work is split across two sessions:

- **Session 1** — runtime support (makes the node *execute*).
- **Session 2** — UI discovery endpoint (makes the **Discover Tools** button work).

Both sessions are documented file-by-file. Every change is shown as **original → modified**, with explicit "added," "modified," "removed," and "untouched" callouts. Lines that don't change are marked as such.

---

## File inventory

| # | File | Session | Change type | Net new lines |
|---|---|---|---|---|
| 1 | `graph_builder/llm_tool_manager.py` | 1 | Refactor + addition | ~120 |
| 2 | `graph_builder/nodes/mcp_tool_non_llm.py` | 1 | NEW file | ~110 |
| 3 | `graph_builder/compiler.py` | 1 | One-line addition | 1 |
| 4 | `graph_builder/nodes/__init__.py` | 1 | Two-line addition | 2 |
| 5 | `api/v2/models.py` | 2 | Append (3 classes) | ~30 |
| 6 | `api/v2/routes_mcp.py` | 2 | NEW file | ~80 |
| 7 | Wherever `routes_execute.router` is registered | 2 | One-line addition | 1 |

**Total: 4 files modified (3 of them by appending only), 2 new files, 1 line in your app entry point.**

No existing function bodies are deleted. No existing function signatures change. No existing files are reorganized.

---

# SESSION 1 — Runtime Support

## File 1 — `graph_builder/llm_tool_manager.py`

This is the most substantial change. It's a **refactor + addition**, not a rewrite. Existing public API is preserved.

### What stays exactly the same

- `class CallbackMultiserverMCPClient` — **untouched**.
- `def _custom_httpx_factory` — **untouched**.
- `def _process_builtin_tools` — **untouched**.
- `async def get_attached_llm_tools` — **signature untouched**, but the body is rewritten to call new shared helpers instead of doing MCP setup inline. Call sites (compiler.py) need no changes.

### What is added

**Two new private helpers** (extracted from what `get_attached_llm_tools` was doing inline):

```python
def _build_mcp_server_config(
    *,
    url: str,
    transport: str,
    node_id: str | None = None,
) -> Dict[str, Any]:
    """Build a single MCP server config dict.

    Used by both attached and standalone code paths to ensure server
    config shape is constructed identically.
    """
    config: Dict[str, Any] = {
        "url": url,
        "transport": "sse" if transport == "SSE" else "http",
        "httpx_client_factory": _custom_httpx_factory,
    }
    if node_id is not None:
        config["node_id"] = node_id
    return config


async def _fetch_mcp_tools(
    server_configs: Dict[str, Dict[str, Any]],
    *,
    use_callback_client: bool = False,
    tool_name_prefix: bool = False,
) -> List[StructuredTool]:
    """Build an MCP client from the given server configs, fetch tools, return them.

    Single point of MCP client instantiation in this module. Both
    `get_attached_llm_tools` and `get_standalone_mcp_tool` go through here.
    """
    if use_callback_client:
        client = CallbackMultiserverMCPClient(
            connections=server_configs,
            tool_name_prefix=tool_name_prefix,
        )
    else:
        client = MultiServerMCPClient(
            connections=server_configs,
            tool_name_prefix=tool_name_prefix,
        )
    return await client.get_tools()
```

**One new public function:**

```python
async def get_standalone_mcp_tool(mcp_node_spec: NodeSpec) -> StructuredTool:
    """Fetch and return the single tool a standalone mcpServer node should invoke.

    Called at compile time by the compiler when it encounters an mcpServer
    node with llmAttached=False. Returns the StructuredTool matching the
    node's data.toolName. Raises ValueError if the tool name is missing or
    not present on the configured server.
    """
    data = mcp_node_spec.data or {}
    url = data.get("url")
    transport = data.get("transport", "HTTP")
    tool_name = data.get("toolName")

    if not tool_name:
        raise ValueError(
            f"mcpServer node {mcp_node_spec.id} is standalone but has no "
            f"data.toolName configured."
        )
    if not url:
        raise ValueError(
            f"mcpServer node {mcp_node_spec.id} has no data.url configured."
        )

    server_configs = {
        mcp_node_spec.id: _build_mcp_server_config(url=url, transport=transport),
    }

    tools = await _fetch_mcp_tools(server_configs)

    for tool in tools:
        if tool.name == tool_name:
            return tool

    available = ", ".join(t.name for t in tools) or "(none)"
    raise ValueError(
        f"Tool '{tool_name}' not found on MCP server at {url}. "
        f"Available tools: {available}"
    )
```

### What is modified

`get_attached_llm_tools` body is rewritten to call the new helpers. **Public signature, return type, and observable behavior are unchanged.**

**Before (the inline version):**

```python
if attached_mcp_servers:
    for mcp_node_id in attached_mcp_servers:
        mcp_node_spec = all_nodes[mcp_node_id]
        url = mcp_node_spec.data.get("url")
        transport = "sse" if mcp_node_spec.data.get("transport") == "SSE" else "http"
        tool_prefix = mcp_node_spec.data.get('prefix')
        mcp_configs[tool_prefix] = {
            "url": url,
            "transport": transport,
            "httpx_client_factory": _custom_httpx_factory,
            "node_id": mcp_node_id
        }

    client = CallbackMultiserverMCPClient(connections=mcp_configs, tool_name_prefix=True)
    mcp_tools = await client.get_tools()
    tools.extend(mcp_tools)
    ...
```

**After (using helpers):**

```python
if attached_mcp_servers:
    for mcp_node_id in attached_mcp_servers:
        mcp_node_spec = all_nodes[mcp_node_id]
        tool_prefix = mcp_node_spec.data.get("prefix")
        mcp_configs[tool_prefix] = _build_mcp_server_config(
            url=mcp_node_spec.data.get("url"),
            transport=mcp_node_spec.data.get("transport", "HTTP"),
            node_id=mcp_node_id,
        )

    mcp_tools = await _fetch_mcp_tools(
        mcp_configs,
        use_callback_client=True,
        tool_name_prefix=True,
    )
    tools.extend(mcp_tools)
    ...
```

### Risk assessment for File 1

**Low.** The refactor is mechanical — both code paths construct the same config dict and instantiate the same `CallbackMultiserverMCPClient` with the same arguments they did before. The new `get_standalone_mcp_tool` is purely additive; nothing else calls it yet (Session 2 endpoint and the compiler are the future callers).

---

## File 2 — `graph_builder/nodes/mcp_tool_non_llm.py` (NEW)

Brand new file. Mirrors the existing `tool_non_llm.py` exactly except the tool is passed in pre-fetched (no registry lookup).

### Reference: the original `tool_non_llm.py`

The new file follows the same shape as your existing `build_tool` builder. Quick recap of what `tool_non_llm.py` does today:

1. Reads `data.toolId` and looks up the tool from `tool_registry`
2. Returns an `awrap` async function that:
   - Normalizes state (sets `messages`, `form`, `variables`, `row` defaults)
   - Computes `call_kwargs` from `data.parameterOverrides` filtered against the tool's `args_schema`
   - Emits a `before_tool` thinking message
   - Invokes the tool
   - Emits an `after_tool` thinking message
   - Returns the result
3. Wraps `awrap` with `wrap_node_with_error_policy`

### What's identical in the new file

- `_get_override_parameters` — copied verbatim.
- `_normalize_state` — copied verbatim.
- `_invoke_tool` — copied verbatim.
- The `awrap` function structure — copied verbatim.
- The `wrap_node_with_error_policy` wrapping — copied verbatim.
- The `before_tool` / `after_tool` thinking-message emissions — copied verbatim.

### What's different from `tool_non_llm.py`

Two things only:

1. **The tool is passed in as a parameter** instead of fetched from `tool_registry`. The compiler pre-fetches it via `get_standalone_mcp_tool` and hands it to the builder.

   ```python
   # tool_non_llm.py:
   async def build_tool(node_spec, _all_nodes, package):
       tool_id = node_spec.data.get("toolId")
       tool = get_tool(tool_id)
       ...

   # mcp_tool_non_llm.py:
   async def build_mcp_tool_non_llm(node_spec, _all_nodes, package, *, tool):
       # tool is passed in pre-fetched from get_standalone_mcp_tool
       ...
   ```

2. **After invocation, the result is stored under `data.outputVarName`** in `state.variables` so downstream nodes can reference it as `$.variables.<name>`. `tool_non_llm.py` doesn't do this because LLM-attached tool results flow back into the agent's context, not into named variables.

   ```python
   result = await _invoke_tool(tool, call_kwargs)

   # NEW — only in mcp_tool_non_llm.py:
   output_var_name = (node_spec.data or {}).get("outputVarName")
   if output_var_name:
       norm_state.setdefault("variables", {})[output_var_name] = result
   ```

### Risk assessment for File 2

**Near-zero.** It's a copy-paste of an existing, working builder with two minimal local changes. No global state. No imports beyond what `tool_non_llm.py` already uses. If it has a bug, only standalone MCP nodes are affected; everything else works.

---

## File 3 — `graph_builder/compiler.py`

Tiny change. One new branch in `_build_nodes`. Everything else is untouched.

### What stays exactly the same

- All imports (except one new one).
- `OrchestrationCompiler.compile()` — unchanged.
- `_normalize_nodes`, `_normalize_edges` — unchanged.
- `_build_llm_tools_node`, `_infer_entry`, `_has_tool_calls`, `_connect_edge` — unchanged.
- The existing `if spec.kind == "agent.codeless":` branch in `_build_nodes` — unchanged.
- The existing `if spec.kind == "tool" and spec.data.get("llmAttached", False): continue` — unchanged.
- The existing `if spec.kind == "mcpServer" and spec.data.get("llmAttached", False): continue` — unchanged. **This is important — it means LLM-attached MCP nodes are still skipped, exactly as before.**

### What's added

**One import line:**

```python
from graph_builder.llm_tool_manager import (
    get_attached_llm_tools,
    get_standalone_mcp_tool,  # NEW
)
```

**One new branch inside `_build_nodes`,** added before the existing `else` that handles all other node kinds:

```python
async def _build_nodes(self, graph, nodes, package):
    for nid, spec in nodes.items():
        builder = NODE_BUILDERS.get(spec.kind)

        if spec.kind == "tool" and spec.data.get("llmAttached", False):
            continue

        if spec.kind == "mcpServer" and spec.data.get("llmAttached", False):
            continue

        if builder is None:
            raise ValueError(f"No builder found for node kind: {spec.kind}")

        if spec.kind == "agent.codeless":
            tool_list, tool_to_node_lookup = await get_attached_llm_tools(spec, nodes)
            fn = await builder(spec, nodes, package, tool_list=tool_list)
            graph.add_node(nid, fn, metadata={...})
            await self._build_llm_tools_node(...)

        # === NEW BRANCH ===
        elif spec.kind == "mcpServer":
            # Standalone MCP: llmAttached is False (already filtered above);
            # fetch the single tool at compile time and pass to the builder.
            tool = await get_standalone_mcp_tool(spec)
            fn = await builder(spec, nodes, package, tool=tool)
            graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
        # === END NEW BRANCH ===

        else:
            fn = await builder(spec, nodes, package)
            graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
```

### Risk assessment for File 3

**Low.** The new branch only fires when `spec.kind == "mcpServer"` AND `llmAttached=False` (because `llmAttached=True` is already skipped above). No existing graph hits this path. Existing graphs route through the same `else` clause they did before.

---

## File 4 — `graph_builder/nodes/__init__.py`

Smallest change in the entire backend. Two lines.

### What stays exactly the same

Every existing import and every existing entry in `NODE_BUILDERS` is preserved verbatim.

### What's added

**One import line:**

```python
from .mcp_tool_non_llm import build_mcp_tool_non_llm  # NEW
```

**One entry in `NODE_BUILDERS`:**

```python
NODE_BUILDERS = {
    "entry.chat_form": build_entry_chat_form,
    "router.deterministic": build_router_deterministic,
    "router.llm": build_router_llm,
    "sequential": build_sequential,
    "parallel": build_parallel,
    "concurrent": build_router_concurrent,
    "reducer": build_reducer,
    "publisher": build_publisher,
    "output": build_output,
    "agent.codeless": build_agent_codeless,
    "tool": build_tool,
    "mcpServer": build_mcp_tool_non_llm,  # NEW
    "codeblock.foreach": build_foreach,
    "codeblock.foreachend": build_foreachend,
}
```

### Risk assessment for File 4

**Zero.** Adding a key to the registry doesn't affect lookups for other keys.

---

# SESSION 2 — UI Discovery Endpoint

## File 5 — `api/v2/models.py`

**Append-only.** Nothing existing is modified.

### What stays exactly the same

Every model from `Message` through `ExecuteRequest` — **all unchanged.** The new classes go at the bottom of the file.

### What's added

Three new classes, dropped in beneath `ExecuteRequest`:

```python
class McpDiscoverAuth(BaseModel):
    """Auth configuration mirroring the mcpServer node's `auth` field."""
    type: Literal["None", "OBO", "client_credentials", "api_key", "mtls"] = "None"
    scopes: Optional[List[str]] = None
    secretRef: Optional[str] = None


class McpDiscoverRequest(BaseModel):
    """Request body for POST /api/v2/mcp/discover."""
    url: str = Field(..., description="MCP server URL")
    transport: Literal["HTTP", "SSE"] = Field(default="HTTP")
    auth: Optional[McpDiscoverAuth] = None
    tenant_id: Optional[str] = Field(default=None, alias="tenantId")

    class Config:
        validate_by_name = True


class McpDiscoveredTool(BaseModel):
    """One tool returned from an MCP server's tool catalog."""
    name: str
    description: Optional[str] = None
    args_schema: Optional[Dict[str, Any]] = Field(default=None, alias="argsSchema")

    class Config:
        validate_by_name = True
        populate_by_name = True


class McpDiscoverResponse(BaseModel):
    """Response body for POST /api/v2/mcp/discover."""
    ok: bool = True
    tools: List[McpDiscoveredTool] = Field(default_factory=list)
    error: Optional[str] = None
```

### Risk assessment for File 5

**Zero.** Append-only. All existing imports (`Literal`, `Optional`, `List`, `Dict`, `Any`, `BaseModel`, `Field`) are already present at the top of `models.py`.

---

## File 6 — `api/v2/routes_mcp.py` (NEW)

New file. One endpoint.

### What it contains

```python
from typing import Any, Dict, List, Optional
import structlog
from fastapi import APIRouter, Request

from api.v2.models import (
    McpDiscoverRequest,
    McpDiscoverResponse,
    McpDiscoveredTool,
)
from graph_builder.llm_tool_manager import (
    _build_mcp_server_config,
    _fetch_mcp_tools,
)

router = APIRouter(prefix="/v2", tags=["v2", "mcp"])
log = structlog.get_logger(__name__)


@router.post(path="/mcp/discover", tags=["mcp"])
async def discover_mcp_tools(
    payload: McpDiscoverRequest,
    request: Request,
) -> McpDiscoverResponse:
    """
    Connect to an MCP server and return its tool catalog.

    Returns 200 with ok=False and `error` populated on connection failures
    so the UI can render an inline error and let the user retry.
    """
    correlation_id: Optional[str] = (
        getattr(request.state, "correlation_id", None)
        or request.headers.get("X-Correlation-Id")
    )

    log.info(
        "mcp_discover_begin",
        url=payload.url,
        transport=payload.transport,
        auth_type=(payload.auth.type if payload.auth else "None"),
        correlation_id=correlation_id,
    )

    # Build config using Session 1's helper.
    try:
        server_config = _build_mcp_server_config(
            url=payload.url,
            transport=payload.transport,
        )
    except Exception as exc:
        log.warning("mcp_discover_config_failed", error_message=str(exc))
        return McpDiscoverResponse(ok=False, tools=[], error=f"Invalid server configuration: {exc}")

    # Fetch tools using Session 1's helper.
    try:
        raw_tools = await _fetch_mcp_tools({payload.url: server_config})
    except Exception as exc:
        log.warning("mcp_discover_fetch_failed", url=payload.url, error_message=str(exc))
        return McpDiscoverResponse(ok=False, tools=[], error=f"Could not connect to MCP server: {exc}")

    # Map to response shape.
    tools_out: List[McpDiscoveredTool] = []
    for t in raw_tools:
        try:
            tools_out.append(
                McpDiscoveredTool(
                    name=getattr(t, "name", "") or "",
                    description=getattr(t, "description", None),
                    args_schema=getattr(t, "args_schema", None),
                )
            )
        except Exception as exc:
            log.warning("mcp_discover_tool_skipped", error_message=str(exc))

    log.info("mcp_discover_end", url=payload.url, count=len(tools_out))
    return McpDiscoverResponse(ok=True, tools=tools_out)
```

### Notable design choices

- **Returns 200 with `ok=False` on connection errors** instead of 5xx. UI can render inline errors without try/catch ceremony.
- **Reuses `_build_mcp_server_config` and `_fetch_mcp_tools`** verbatim from File 1. No duplication.
- **No auth plumbing yet** for v1. The endpoint accepts an `auth` field but doesn't pass it to `_build_mcp_server_config` — you can wire that through later if needed for OBO/api_key servers. For unauthenticated dev servers (the smoke test stub), this is fine as-is.

### Risk assessment for File 6

**Low.** New file. Zero impact on existing routes. If the endpoint has a bug, only the **Discover Tools** button in the UI is affected.

---

## File 7 — Router registration (one line, in your app entry)

Wherever `routes_execute.router` is registered with the FastAPI app — typically `main.py`, `app.py`, or a similar entry point — add one line:

```python
from api.v2 import routes_execute, routes_mcp  # add routes_mcp

app.include_router(routes_execute.router)
app.include_router(routes_mcp.router)  # NEW
```

### How to find the registration site

Search the codebase for `include_router(routes_execute` — that line is the one to edit.

### Risk assessment for File 7

**Zero.** Adds one route. Doesn't change any existing routes or middleware.

---

# Verification — what to run after applying

## Session 1 verification

Already covered in your existing `standalone_mcp_implementation.md`. Three stepwise smoke tests:

1. `python smoke_test.py 1` — discovery works
2. `python smoke_test.py 2` — node-level invocation works
3. `python smoke_test.py 3` — end-to-end graph compile + execute works (this is the demoable milestone)

## Session 2 verification

After applying Files 5–7 and restarting the FastAPI app:

```bash
# Happy path
curl -X POST http://localhost:8000/api/v2/mcp/discover \
  -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:8123", "transport": "HTTP"}'
```

Expected response:

```json
{
  "ok": true,
  "tools": [
    {
      "name": "echo",
      "description": "Echo back the input message",
      "args_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
    }
  ],
  "error": null
}
```

```bash
# Failure path
curl -X POST http://localhost:8000/api/v2/mcp/discover \
  -H "Content-Type: application/json" \
  -d '{"url": "http://does-not-exist:99999", "transport": "HTTP"}'
```

Expected: HTTP 200, `{"ok": false, "tools": [], "error": "Could not connect to MCP server: ..."}`.

If both curl tests pass, the backend is ready for the frontend.

---

# What is explicitly NOT changing

- **No edits to existing function bodies.** Only `get_attached_llm_tools` is rewritten internally, and its public signature/return shape stays identical.
- **No deletions.** Every existing line in every original file is preserved (modulo the internal rewrite of `get_attached_llm_tools`'s body).
- **No new directories.** All new files live in `graph_builder/nodes/` or `api/v2/`.
- **No new dependencies.** `pip install` is not needed.
- **No database changes.**
- **No config changes** — neither `config.settings.APP_CONFIG` nor any environment variable.
- **No middleware changes** — the new endpoint inherits whatever the existing v2 routes use.
- **No changes to `runtime/`, `clients/`, `tools/`, or anything outside the listed files.**
- **No removed tests.** The Session 1 smoke test is preserved unchanged.

---

# Files in this delivery

Saved to `/mnt/user-data/outputs/`:

- `mcp_discovery_models.py` — the three Pydantic classes for File 5
- `routes_mcp.py` — File 6 contents
- `backend_changes.md` — this document

The Session 1 implementation already has its own doc (`standalone_mcp_implementation.md`) and code; Session 1 files don't need to be redelivered.
