# Standalone MCP Tool Node — Implementation Package

This document describes a feature for a LangGraph-based orchestration platform: adding support for invoking a single MCP server tool as a deterministic pipeline step (not bound to an LLM). The platform already supports MCP servers in LLM-attached mode (where the LLM picks which tool to call); this work extends it so a graph author can drop a specific MCP tool onto the canvas and run it as a fixed step in the graph.

The patterns shown here assume an existing codebase structure with:

- A node builder registry (`NODE_BUILDERS`) keyed by node `kind`
- A separate `tool_llm.py` / `tool_non_llm.py` split for regular tools (LLM-attached vs. standalone)
- A compiler that walks the orchestration package and registers nodes with a LangGraph `StateGraph`
- A `wrap_node_with_error_policy` helper that handles lifecycle events, retry, and state mutation
- Existing MCP integration via `langchain_mcp_adapters.MultiServerMCPClient`, with all MCP-specific code centralized in an `llm_tool_manager.py` module

Adapt names and paths to match your codebase.

## Contents

1. [Overview](#overview)
2. [Implementation order](#implementation-order)
3. [File 1 — `llm_tool_manager.py` refactor + addition](#file-1--llm_tool_managerpy-refactor--addition)
4. [File 2 — `nodes/mcp_tool_non_llm.py` (new file)](#file-2--nodesmcp_tool_non_llmpy-new-file)
5. [File 3 — `compiler.py` changes](#file-3--compilerpy-changes)
6. [File 4 — `nodes/__init__.py` update](#file-4--nodes__init__py-update)
7. [Smoke test stub MCP server](#smoke-test-stub-mcp-server)
8. [Smoke test harness](#smoke-test-harness)
9. [Things to verify when running](#things-to-verify-when-running)
10. [Things deliberately deferred for v1](#things-deliberately-deferred-for-v1)

---

## Overview

Adds a standalone (non-LLM-attached) execution mode to the existing `mcpServer` node kind. When `llmAttached=False`, the node represents one specific MCP tool invocation as a deterministic pipeline step — parameters supplied through node config, no LLM in the loop.

**Design decisions locked for v1:**

- **Reuse the existing `mcpServer` kind.** The compiler already branches on it; the `llmAttached` flag becomes the mode switch (mirrors how `tool` works).
- **Compile-time tool discovery.** Mirrors how LLM-attached MCP tools are fetched today — the compiler calls into `llm_tool_manager` and passes the pre-fetched tool to the builder.
- **Separate file `nodes/mcp_tool_non_llm.py`.** Parallels the `tool_llm.py` / `tool_non_llm.py` split.
- **Shared helpers in `llm_tool_manager`.** Rather than duplicating MCP client setup between the attached and standalone code paths, the public functions (`get_attached_llm_tools` and `get_standalone_mcp_tool`) both go through small private helpers (`_build_mcp_server_config`, `_fetch_mcp_tools`). This keeps the single-MCP-boundary principle intact and gives one place to add client caching or session reuse later.
- **No `discoveryMode` flag yet.** If runtime discovery is ever needed, add it later. Keep v1 PR small.

---

## Implementation order

Apply changes and run the smoke test stepwise:

1. Refactor `llm_tool_manager.py` to introduce shared helpers and add `get_standalone_mcp_tool`.
2. Start the stub MCP server (see [Smoke test stub MCP server](#smoke-test-stub-mcp-server)).
3. Run `python smoke_test.py 1` — verifies discovery works.
4. Create `nodes/mcp_tool_non_llm.py`.
5. Update `nodes/__init__.py`.
6. Run `python smoke_test.py 2` — verifies node-level invocation.
7. Apply the compiler diff.
8. Run `python smoke_test.py 3` — verifies end-to-end graph compilation and execution. **This is the demoable milestone.**

---

## File 1 — `llm_tool_manager.py` refactor + addition

The refactor introduces two private helpers that consolidate MCP client setup, then both public functions (existing `get_attached_llm_tools` and new `get_standalone_mcp_tool`) become thinner.

### Why the refactor

Before this change, `get_attached_llm_tools` directly instantiates a `MultiServerMCPClient` and constructs per-server config dicts inline. Adding `get_standalone_mcp_tool` would duplicate both patterns. Instead, extract them so:

- The MCP client is instantiated in exactly one place (`_fetch_mcp_tools`).
- The per-server config dict is built in exactly one place (`_build_mcp_server_config`).
- Future evolution (client caching, transport changes, session reuse) happens in those helpers and benefits both code paths.

This is a small refactor with near-zero behavioral risk — both existing call sites end up calling the same `MultiServerMCPClient` with the same arguments they did before.

### The full refactored file

```python
import copy
from typing import Any, Dict, List, Tuple

from clients.http_client import get_http_client_async
from graph_builder.types import NodeSpec
from tools.tool_registry import get_tool
from tools.types import ToolSpec

from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient


class CallbackMultiserverMCPClient(MultiServerMCPClient):
    """Wraps MultiServerMCPClient to attach mcp_server_name and node_id metadata
    to each tool returned. Used by the LLM-attached path so the runtime knows
    which MCP server (and graph node) each tool came from when the LLM picks
    one to call.
    """
    def __init__(self, *args, **kwargs):
        self._label_to_node_id = {}

        connections = kwargs.get('connections')
        for label, config in connections.items():
            if 'node_id' in config:
                self._label_to_node_id[label] = config.pop('node_id')

        super().__init__(*args, **kwargs)

    async def get_tools(self):
        tools = await super().get_tools()

        for tool in tools:
            if tool.metadata is None:
                tool.metadata = {}
            for mcp_server_name in self.connections.keys():
                if tool.name.startswith(mcp_server_name):
                    tool.metadata['mcp_server_name'] = mcp_server_name
                    tool.metadata['node_id'] = self._label_to_node_id.get(mcp_server_name)
                    break

        return tools


def _custom_httpx_factory(headers=None, timeout=None, auth=None):
    return get_http_client_async(headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# Shared internal helpers — single point of MCP client/config construction
# ---------------------------------------------------------------------------

def _build_mcp_server_config(
    *,
    url: str,
    transport: str,
    node_id: str | None = None,
) -> Dict[str, Any]:
    """Build a single MCP server config dict.

    Used by both attached and standalone code paths to ensure server config
    shape is constructed identically. If transport handling ever needs to
    evolve (e.g. supporting streamable_http natively), it changes here.

    Args:
        url: MCP server URL.
        transport: Raw transport string from node data ("SSE" or anything
            else, which is treated as HTTP).
        node_id: Optional graph node id to embed in the config. The
            CallbackMultiserverMCPClient extracts this and uses it to attach
            tool metadata. The standalone path doesn't need this.

    Returns:
        Config dict suitable for inclusion in a MultiServerMCPClient
        connections argument.
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
    Future client caching, session reuse, or transport-layer changes happen
    in this function and benefit both code paths.

    Args:
        server_configs: Dict of {server_label: server_config_dict}.
        use_callback_client: If True, use CallbackMultiserverMCPClient (which
            attaches mcp_server_name/node_id metadata to each returned tool).
            The attached path needs this for tool→server mapping; the
            standalone path doesn't.
        tool_name_prefix: If True, MultiServerMCPClient prefixes each tool
            name with its server label. The attached path uses this to avoid
            name collisions across multiple servers; the standalone path
            doesn't (single tool, single server, no collision risk).

    Returns:
        Flat list of StructuredTool objects from all servers in server_configs.
    """
    if use_callback_client:
        client = CallbackMultiserverMCPClient(
            connections=server_configs,
            tool_name_prefix=tool_name_prefix,
        )
    else:
        client = MultiServerMCPClient(connections=server_configs)

    return await client.get_tools()


# ---------------------------------------------------------------------------
# Builtin (non-MCP) tools processing
# ---------------------------------------------------------------------------

def _process_builtin_tools(attached_builtin_tools, all_nodes, tools, tool_to_node_lookup):
    """Process regular (non-MCP) tools attached to an LLM node."""
    for tool_node_id in attached_builtin_tools:
        tool_node_spec = all_nodes[tool_node_id]

        if tool_node_spec.kind == "tool":
            tool_prefix = tool_node_spec.data.get('prefix')
            tool_id = tool_node_spec.data.get("toolId")
            tool = get_tool(tool_id)

            if tool is None:
                raise ValueError(f"Unknown tool id: {tool_id}")

            tool = copy.deepcopy(tool)
            tool.name = tool_prefix + '_' + tool.name if tool_prefix else tool.name
            tools.append(tool)
            tool_to_node_lookup[tool.name] = tool_node_id


# ---------------------------------------------------------------------------
# Public — fetch tools attached to an LLM node (existing function, refactored)
# ---------------------------------------------------------------------------

async def get_attached_llm_tools(
    llm_node_spec: NodeSpec,
    all_nodes: Dict[str, NodeSpec],
) -> Tuple[List[ToolSpec], Dict[str, str]]:
    """Fetch all tools (builtin + MCP) attached to an LLM node.

    Returns the flat tool list plus a tool_name → graph_node_id lookup so the
    runtime can route tool calls back to the correct source node.
    """
    tools: List[ToolSpec] = []
    tool_to_node_lookup: Dict[str, str] = {}
    mcp_configs: Dict[str, Dict[str, Any]] = {}

    attached_builtin_tools = llm_node_spec.data.get("tools", {}).get("attached", [])
    attached_mcp_servers = llm_node_spec.data.get("mcpServers", {}).get("attached", [])

    _process_builtin_tools(attached_builtin_tools, all_nodes, tools, tool_to_node_lookup)

    if attached_mcp_servers:
        for mcp_node_id in attached_mcp_servers:
            mcp_node_spec = all_nodes[mcp_node_id]
            tool_prefix = mcp_node_spec.data.get('prefix')
            mcp_configs[tool_prefix] = _build_mcp_server_config(
                url=mcp_node_spec.data.get("url"),
                transport=mcp_node_spec.data.get("transport"),
                node_id=mcp_node_id,
            )

        mcp_tools = await _fetch_mcp_tools(
            mcp_configs,
            use_callback_client=True,
            tool_name_prefix=True,
        )
        tools.extend(mcp_tools)

        for tool in mcp_tools:
            if tool.metadata and 'mcp_server_name' in tool.metadata:
                tool_to_node_lookup[tool.name] = tool.metadata["node_id"]

    return tools, tool_to_node_lookup


# ---------------------------------------------------------------------------
# Public — fetch a single MCP tool for a standalone mcpServer node (NEW)
# ---------------------------------------------------------------------------

async def get_standalone_mcp_tool(mcp_node_spec: NodeSpec) -> StructuredTool:
    """Fetch a single MCP tool for a standalone mcpServer node (llmAttached=False).

    Mirrors `get_attached_llm_tools` but:
    - Scoped to one server (not a list of attached servers).
    - Returns a single tool object selected by name from the server's catalog.
    - Does NOT use tool_name_prefix — the node knows exactly which tool it
      wants, so prefixing buys nothing and complicates name matching.
    - Does NOT use CallbackMultiserverMCPClient — no need for tool→server
      metadata attachment when the node is pinned to one specific tool.

    Called from the compiler at compile time, parallel to how attached MCP
    tools are fetched for LLM nodes.

    Args:
        mcp_node_spec: NodeSpec for an mcpServer node with llmAttached=False.
            Expected `data` fields:
            - url (str): MCP server URL
            - transport (str): "SSE" → "sse", anything else → "http"
            - toolName (str): the specific tool to fetch from the server

    Returns:
        The StructuredTool matching `data["toolName"]`.

    Raises:
        ValueError: if `url` or `toolName` is missing, or if no tool with
            that name exists on the configured server.
    """
    data = mcp_node_spec.data or {}
    url = data.get("url")
    tool_name = data.get("toolName")
    transport = data.get("transport")

    if not url:
        raise ValueError(
            f"Standalone MCP node '{mcp_node_spec.id}' is missing required "
            f"'url' field in data."
        )
    if not tool_name:
        raise ValueError(
            f"Standalone MCP node '{mcp_node_spec.id}' is missing required "
            f"'toolName' field in data. Set this to the name of the specific "
            f"tool from the MCP server that this node should invoke."
        )

    server_config = _build_mcp_server_config(url=url, transport=transport)

    # Single-server config keyed by node id (the key is internal — we're not
    # exposing it via prefixes since standalone nodes invoke a single known
    # tool).
    tools = await _fetch_mcp_tools({mcp_node_spec.id: server_config})

    matching = next((t for t in tools if t.name == tool_name), None)
    if matching is None:
        available = ", ".join(sorted(t.name for t in tools)) or "(none)"
        raise ValueError(
            f"Standalone MCP node '{mcp_node_spec.id}' references tool "
            f"'{tool_name}' which does not exist on the MCP server at {url}. "
            f"Available tools: {available}"
        )

    return matching
```

### What this refactor changes about existing behavior

Nothing meaningful. `get_attached_llm_tools` ends up calling `MultiServerMCPClient` (as `CallbackMultiserverMCPClient`) with `tool_name_prefix=True` and the same connections dict it built before. The intermediate steps go through helpers, but the final call shape is identical.

### What it sets up for the future

- A single function (`_fetch_mcp_tools`) where client caching, connection pooling, or session reuse can be added when needed.
- A single function (`_build_mcp_server_config`) where transport handling can evolve.
- Any future MCP-fetching public function (e.g. `get_tools_for_server` for a UI discovery endpoint) becomes a four-line wrapper over the helpers.

---

## File 2 — `nodes/mcp_tool_non_llm.py` (new file)

Create this file at `graph_builder/nodes/mcp_tool_non_llm.py`. Mirrors `tool_non_llm.py` almost line-for-line. The only structural difference: `tool` is a keyword-only parameter passed in by the compiler instead of being looked up via `get_tool(tool_id)`.

```python
"""
graph_builder/nodes/mcp_tool_non_llm.py

Standalone (non-LLM-attached) MCP tool node builder.

Mirrors the shape of `tool_non_llm.py`. The key differences:

1. The tool object is passed in pre-fetched (by the compiler) rather than
   looked up from the local tool registry. The compiler calls
   `llm_tool_manager.get_standalone_mcp_tool(node_spec)` at compile time and
   passes the resulting tool here.

2. There is no `toolId` lookup — the MCP tool was already discovered and
   matched by name when the compiler called the MCP server.

Everything else (parameter overrides, state injection, lifecycle wrapping)
matches the regular non-LLM tool node exactly.
"""
from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from api.v2.models import OrchestrationPackage
from graph_builder.node_wrapper import wrap_node_with_error_policy
from graph_builder.types import NodeSpec
from runtime.models import OrchestrationState
from tools.variable_transformer import replace_vars_in_str


def _get_override_parameters(
    data: Any, tool: Any, state: OrchestrationState | None = None
) -> Dict[str, Any]:
    """Build the call kwargs from `data["parameterOverrides"]`, filtered by the
    tool's args_schema and with variable substitution applied to string values.

    Mirrors `_get_override_parameters` in tool_non_llm.py.
    """
    overrides: Dict[str, Any] = (data or {}).get("parameterOverrides") or {}

    def _tv(val: Any) -> Any:
        return replace_vars_in_str(state, val) if isinstance(val, str) else val

    # MCP tools land here — they are always StructuredTool with args_schema.
    if isinstance(tool, StructuredTool) and getattr(tool, "args_schema", None):
        field_names = set(tool.args_schema.model_fields.keys())
        call_kwargs = {k: _tv(v) for k, v in overrides.items() if k in field_names}

        if state is not None and "state" in field_names and "state" not in call_kwargs:
            call_kwargs["state"] = state
        return call_kwargs

    # Defensive fallback. MCP tools should always be StructuredTool, but be
    # tolerant if that assumption ever changes.
    try:
        if not callable(tool):
            return dict(overrides)

        sig = inspect.signature(tool)
        params = sig.parameters
        has_var_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

        call_kwargs = {
            k: _tv(v)
            for k, v in overrides.items()
            if k in params or has_var_kwargs
        }

        if (
            state is not None
            and "state" not in call_kwargs
            and ("state" in params or has_var_kwargs)
        ):
            call_kwargs["state"] = state
        return call_kwargs
    except (ValueError, TypeError):
        return dict(overrides)


def _normalize_state(state: OrchestrationState) -> OrchestrationState:
    """Ensure the standard state keys exist. Same as tool_non_llm.py."""
    if isinstance(state, dict):
        state.setdefault("messages", [])
        state.setdefault("form", {})
        state.setdefault("variables", {})
        state.setdefault("row", {})
    return state


async def _invoke_tool(tool: Any, call_kwargs: Dict[str, Any]):
    """Invoke the tool. MCP tools are StructuredTool with ainvoke."""
    if isinstance(tool, StructuredTool):
        payload = call_kwargs or {}
        if hasattr(tool, "ainvoke"):
            return await tool.ainvoke(payload)
        return tool.invoke(payload)

    # Defensive fallback.
    result = tool(**call_kwargs) if call_kwargs else tool()
    if inspect.isawaitable(result):
        result = await result
    return result


async def build_mcp_tool_non_llm(
    node_spec: NodeSpec,
    _all_nodes: Dict[str, NodeSpec],
    package: OrchestrationPackage,
    *,
    tool: StructuredTool,
):
    """Build a standalone MCP tool node.

    Args:
        node_spec: The mcpServer node spec (llmAttached=False).
        _all_nodes: Other nodes in the graph (unused; kept for signature
            consistency with other builders).
        package: The orchestration package, needed for error policy resolution.
        tool: The pre-fetched MCP tool, supplied by the compiler via
            `llm_tool_manager.get_standalone_mcp_tool(node_spec)`.

    Returns:
        The error-policy-wrapped node function ready to register with LangGraph.
    """
    data = node_spec.data

    async def awrap(
        state: OrchestrationState,
        emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
        config: RunnableConfig,
    ):
        norm_state = _normalize_state(state)
        call_kwargs = _get_override_parameters(data, tool, norm_state)
        state_for_msg = {**norm_state, "parameters": call_kwargs}

        await emit_thinking_msg(
            state_for_msg,
            "before_tool",
            node_spec.beforeExecuteStatus,
            node_spec.beforeExecuteStatusClearPrevious,
        )

        result = await _invoke_tool(tool, call_kwargs)

        await emit_thinking_msg(
            state_for_msg,
            "after_tool",
            node_spec.afterExecuteStatus,
            node_spec.afterExecuteStatusClearPrevious,
        )

        return result

    return wrap_node_with_error_policy(node_spec, package, awrap)
```

---

## File 3 — `compiler.py` changes

Two edits in `graph_builder/compiler.py`.

### Edit 1 — Update import at the top of the file

```python
# BEFORE
from graph_builder.llm_tool_manager import get_attached_llm_tools

# AFTER
from graph_builder.llm_tool_manager import get_attached_llm_tools, get_standalone_mcp_tool
```

### Edit 2 — Add the standalone MCP branch in `_build_nodes`

Replace the body of `_build_nodes` with this. The change is the new `elif spec.kind == "mcpServer":` block — sits parallel to the `agent.codeless` branch.

```python
async def _build_nodes(self, graph: StateGraph, nodes: Dict[str, NodeSpec], package: OrchestrationPackage):
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
            graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
            await self._build_llm_tools_node(graph, spec, nodes, package, tool_list=tool_list, tool_to_node_lookup=tool_to_node_lookup)

        elif spec.kind == "mcpServer":
            # Standalone MCP tool node: llmAttached is False (the True branch
            # was skipped above). Fetch the specific tool at compile time —
            # mirrors the agent.codeless pattern where MCP work happens here,
            # not at runtime.
            tool = await get_standalone_mcp_tool(spec)
            fn = await builder(spec, nodes, package, tool=tool)
            graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})

        else:
            fn = await builder(spec, nodes, package)
            graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
```

### What does NOT change

- The skip for `mcpServer` + `llmAttached=True` stays. LLM-attached MCP nodes are still handled inside `agent.codeless` via the existing attached-tools fetch.
- `_build_llm_tools_node` is untouched.
- `_connect_edge`, `_normalize_nodes`, `_normalize_edges`, `_infer_entry` are untouched.

---

## File 4 — `nodes/__init__.py` update

Two changes: a new import and a new entry in `NODE_BUILDERS`.

```python
from .entry_chat_form import build_entry_chat_form
from .router_deterministic import build_router_deterministic
from .router_llm import build_router_llm
from .sequential import build_sequential
from .parallel import build_parallel
from .router_concurrent import build_router_concurrent
from .reducer import build_reducer
from .publisher import build_publisher
from .output import build_output
from .agent_codeless import build_agent_codeless
from .tool_non_llm import build_tool
from .mcp_tool_non_llm import build_mcp_tool_non_llm  # NEW
from .foreach import build_foreach, build_foreachend


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
    "mcpServer": build_mcp_tool_non_llm,  # NEW — standalone MCP tool node
    "codeblock.foreach": build_foreach,
    "codeblock.foreachend": build_foreachend,
}
```

---

## Smoke test stub MCP server

A tiny FastMCP server exposing two trivial tools. Self-contained, runs locally, no external dependencies. Use this as the smoke test target so the test is reproducible and doesn't require any specific MCP infrastructure.

Save as `stub_mcp_server.py`:

```python
"""
stub_mcp_server.py

Minimal local MCP server for smoke-testing the standalone MCP tool node.

Exposes two trivial tools:
  - ping()                 → returns "pong"
  - echo(message: str)     → returns the message it was given

Run:
    pip install fastmcp
    python stub_mcp_server.py

Then point the smoke test at http://localhost:8765/mcp
"""

from fastmcp import FastMCP

mcp = FastMCP("smoke-test-stub")


@mcp.tool()
def ping() -> str:
    """Returns 'pong'. Smoke-test tool with no inputs."""
    return "pong"


@mcp.tool()
def echo(message: str) -> str:
    """Echoes the input message back. Smoke-test tool with one string input."""
    return f"echo: {message}"


if __name__ == "__main__":
    # Streamable HTTP transport on localhost.
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8765)
```

---

## Smoke test harness

Save as `smoke_test.py` in the repo root. Run pieces individually as you wire things up:

```bash
# After implementing get_standalone_mcp_tool:
python smoke_test.py 1

# After creating mcp_tool_non_llm.py and updating __init__.py:
python smoke_test.py 2

# After applying the compiler diff:
python smoke_test.py 3

# Or run all three at once:
python smoke_test.py
```

```python
"""
smoke_test.py

Smoke test for the standalone MCP tool node feature.

Targets the local stub MCP server (stub_mcp_server.py). Start that first:
    python stub_mcp_server.py

Then run smoke tests stepwise:
    python smoke_test.py 1   # discovery
    python smoke_test.py 2   # node-level invocation
    python smoke_test.py 3   # end-to-end graph compilation + execution
    python smoke_test.py     # all three
"""

import asyncio
import json
import sys

# ---------------------------------------------------------------------------
# CONFIG — points at the local stub server
# ---------------------------------------------------------------------------

STUB_URL = "http://127.0.0.1:8765/mcp"
STUB_TRANSPORT = "HTTP"  # follows the data["transport"] convention; "HTTP" → "http"

# Use the echo tool so we exercise parameter passing.
TEST_TOOL_NAME = "echo"
TEST_TOOL_ARGS = {"message": "hello from standalone MCP node"}


# ---------------------------------------------------------------------------
# Step 1 — fetch a single tool from the MCP server
# ---------------------------------------------------------------------------

async def test_step_1_fetch_tool():
    """Verify get_standalone_mcp_tool fetches the right tool from the server."""
    print("\n=== Step 1: Fetch tool from MCP server ===")

    from graph_builder.llm_tool_manager import get_standalone_mcp_tool
    from graph_builder.types import NodeSpec

    spec = NodeSpec(
        id="smoke_test_node",
        kind="mcpServer",
        label="Smoke Test MCP Node",
        description=None,
        reportStatus=False,
        beforeExecuteStatus=None,
        beforeExecuteStatusClearPrevious=None,
        afterExecuteStatus=None,
        afterExecuteStatusClearPrevious=None,
        data={
            "url": STUB_URL,
            "transport": STUB_TRANSPORT,
            "toolName": TEST_TOOL_NAME,
        },
        io={},
    )

    tool = await get_standalone_mcp_tool(spec)

    print(f"  Tool name:        {tool.name}")
    print(f"  Tool description: {tool.description}")
    print(f"  args_schema:      {tool.args_schema.__name__ if tool.args_schema else None}")
    if tool.args_schema:
        print(f"  Expected inputs:")
        print(f"    {json.dumps(tool.args_schema.model_json_schema(), indent=4)}")

    assert tool.name == TEST_TOOL_NAME, f"Expected {TEST_TOOL_NAME}, got {tool.name}"
    print("  Step 1 passed.")
    return tool


async def test_step_1_missing_tool():
    """Verify clear error when toolName doesn't exist on the server."""
    print("\n=== Step 1b: Missing tool error path ===")

    from graph_builder.llm_tool_manager import get_standalone_mcp_tool
    from graph_builder.types import NodeSpec

    spec = NodeSpec(
        id="smoke_test_node",
        kind="mcpServer",
        label="Smoke Test MCP Node",
        description=None,
        reportStatus=False,
        beforeExecuteStatus=None,
        beforeExecuteStatusClearPrevious=None,
        afterExecuteStatus=None,
        afterExecuteStatusClearPrevious=None,
        data={
            "url": STUB_URL,
            "transport": STUB_TRANSPORT,
            "toolName": "this_tool_definitely_does_not_exist",
        },
        io={},
    )

    try:
        await get_standalone_mcp_tool(spec)
        print("  Expected ValueError, got nothing — FAIL.")
        return False
    except ValueError as e:
        print(f"  Got expected ValueError: {e}")
        print("  Step 1b passed.")
        return True


# ---------------------------------------------------------------------------
# Step 2 — build the node and invoke the tool through it
# ---------------------------------------------------------------------------

async def test_step_2_build_and_invoke():
    """Verify the new node builder invokes the tool and returns a result."""
    print("\n=== Step 2: Build node and invoke tool ===")

    from api.v2.models import Meta, OrchestrationPackage
    from graph_builder.llm_tool_manager import get_standalone_mcp_tool
    from graph_builder.nodes.mcp_tool_non_llm import build_mcp_tool_non_llm
    from graph_builder.types import NodeSpec

    spec = NodeSpec(
        id="smoke_test_node",
        kind="mcpServer",
        label="Smoke Test MCP Node",
        description=None,
        reportStatus=False,
        beforeExecuteStatus=None,
        beforeExecuteStatusClearPrevious=None,
        afterExecuteStatus=None,
        afterExecuteStatusClearPrevious=None,
        data={
            "url": STUB_URL,
            "transport": STUB_TRANSPORT,
            "toolName": TEST_TOOL_NAME,
            "parameterOverrides": TEST_TOOL_ARGS,
            "outputVarName": "smoke_result",
        },
        io={},
    )

    package = OrchestrationPackage(
        meta=Meta(id="smoke", name="smoke", version="0"),
        nodes=[],
        edges=[],
    )

    tool = await get_standalone_mcp_tool(spec)
    fn = await build_mcp_tool_non_llm(spec, {}, package, tool=tool)

    state = {"variables": {}, "messages": [], "form": {}, "row": {}}
    config = {}

    result = await fn(state, config)

    print(f"  Result type: {type(result).__name__}")
    print(f"  Result:      {result}")
    print("  Step 2 passed (tool invoked, result returned).")
    return result


# ---------------------------------------------------------------------------
# Step 3 — compile a minimal graph containing the standalone MCP node and run it
# ---------------------------------------------------------------------------

async def test_step_3_compile_and_run_graph():
    """End-to-end: compile a graph with the new node and execute it."""
    print("\n=== Step 3: Compile and run end-to-end graph ===")

    from graph_builder.compiler import OrchestrationCompiler

    package_dict = {
        "meta": {
            "id": "smoke-pkg",
            "name": "Smoke Test Package",
            "version": "1.0.0",
        },
        "nodes": [
            {
                "id": "mcp_node",
                "kind": "mcpServer",
                "label": "Standalone MCP Tool",
                "data": {
                    "url": STUB_URL,
                    "transport": STUB_TRANSPORT,
                    "toolName": TEST_TOOL_NAME,
                    "parameterOverrides": TEST_TOOL_ARGS,
                    "outputVarName": "smoke_result",
                    "llmAttached": False,
                },
            },
        ],
        "edges": [],
    }

    compiler = OrchestrationCompiler()
    compile_result = await compiler.compile(package_dict)

    print(f"  Compiled. Nodes: {list(compile_result.nodes.keys())}")

    initial_state = {
        "variables": {},
        "messages": [],
        "form": {},
        "row": {},
    }
    config = {"configurable": {"thread_id": "smoke-thread"}}

    final_state = await compile_result.app.ainvoke(initial_state, config=config)

    print(f"  Final state keys:      {list(final_state.keys())}")
    print(f"  Final variables:       {final_state.get('variables', {})}")
    print(f"  smoke_result in vars:  {'smoke_result' in final_state.get('variables', {})}")

    assert "smoke_result" in final_state.get("variables", {}), (
        "Expected 'smoke_result' to be set in state.variables — "
        "the wrapper should have stored the tool result there based on outputVarName."
    )
    print("  Step 3 passed (graph compiled, node ran, result in state).")
    return final_state


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def main():
    step = sys.argv[1] if len(sys.argv) > 1 else "all"

    if step in ("1", "all"):
        await test_step_1_fetch_tool()
        await test_step_1_missing_tool()
    if step in ("2", "all"):
        await test_step_2_build_and_invoke()
    if step in ("3", "all"):
        await test_step_3_compile_and_run_graph()

    print("\nAll requested smoke tests passed.\n")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Things to verify when running

- The `NodeSpec` constructor in the smoke test assumes the same keyword args as what the compiler builds in its node-normalization step. If `NodeSpec` is a dataclass with required fields not covered, adjust accordingly — the test will fail at construction time and tell you what's missing.
- The `MultiServerMCPClient` connection key is `mcp_node_spec.id` — internal-only, since `tool_name_prefix` is not set. Swap if you'd rather use something else.
- The transport string mapping (`"SSE"` → `"sse"`, anything else → `"http"`) follows the existing convention. If the stub server needs a different transport string than `"http"`, adjust either the smoke test or the mapping in `_build_mcp_server_config`.
- `fastmcp` package — install with `pip install fastmcp` if not already available. The stub server is the only thing that depends on it; the production code does not.

---

## Things deliberately deferred for v1

- **No `discoveryMode` flag.** Compile-time only. Runtime discovery can be added later if a real use case demands it.
- **No discovery REST endpoint.** That's UI-facing work and comes after backend ships.
- **No formal `pytest` test suite.** Smoke test is the v1 feedback loop. Real `pytest` tests follow once the implementation is settled.
- **No additional compile-time validation pass.** `get_standalone_mcp_tool` already fails compile with a clear error if `toolName` is missing or doesn't exist on the configured server. That's enough for v1.
- **No client caching or session reuse.** Each call to `_fetch_mcp_tools` instantiates a fresh `MultiServerMCPClient`. Both code paths (attached and standalone) inherit this behavior. Because both paths now go through the same `_fetch_mcp_tools` helper, when a client-lifecycle solution is added (caching, session reuse, connection pooling) it can be implemented once in that helper and benefit both code paths automatically.
- **No frontend work.** Backend ships first. The end-to-end smoke test (step 3) is the demoable milestone — works from a YAML/JSON package with no UI required.
