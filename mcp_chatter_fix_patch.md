# MCP Session Chatter Fix — Before/After Patch

**Status:** Ready to apply
**Files touched:** 3 (`llm_tool_manager.py`, `compiler.py`, `tool_llm.py`)
**Files not touched:** `agent_codeless.py`, `node_wrapper.py`, `http_client.py`, everything else

## Problem

`langchain-mcp-adapters`'s `MultiServerMCPClient` is stateless by default. Every `.ainvoke()` on a tool it produces opens a fresh MCP session: initialize → tools/list → tools/call → teardown. For N tool calls in one LLM turn, that's N full session lifecycles instead of the minimum (1 initialize + 1 tools/list + N tools/call).

## Architectural conclusion

The fix belongs in `tool_llm.py` at the point where `tools` enters `ToolNode(...)`, not in `agent_codeless.py`. Tool execution happens in the downstream tool node, not the LLM agent node.

## Design constraints

- **Session scope:** one session per attached MCP server, per tool-node invocation (one LLM turn).
- **Keep unchanged:** `awrap` telemetry hooks, `override_parameters`, `_is_duplicate_tool_call`, duplicate detection, payload construction, `Command(update={}, goto=Send(...))` routing.
- **`prefix` correction:** `prefix` must not be stuffed into `mcp_configs` because `CallbackMultiserverMCPClient` passes that dict into the underlying client, which doesn't accept arbitrary keys. Split into `mcp_configs` (client-consumable) + `mcp_runtime_info` (metadata like `prefix` and `node_id`).
- **`AsyncExitStack` is required** so sessions stay alive across the boundary between tool loading and `ToolNode.ainvoke(state)`. Using `async with client.session(...)` inside a helper that returns only tools causes the sessions to close before invocation.

---

# File 1: `graph_builder/llm_tool_manager.py`

## BEFORE

```python
async def get_attached_llm_tools(llm_node_spec: NodeSpec, all_nodes: Dict[str, NodeSpec]) -> Tuple[List[ToolSpec], Dict[str,str]]:

    tools: List[ToolSpec] = []
    tool_to_node_lookup: Dict[str, str] = {}
    mcp_configs = {}  # Map tool names to their interceptor configs
    attached_builtin_tools = llm_node_spec.data.get("tools", {}).get("attached", [])
    attached_mcp_servers = llm_node_spec.data.get("mcpServers", {}).get("attached", [])

    _process_builtin_tools(attached_builtin_tools, all_nodes, tools, tool_to_node_lookup)

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

        for tool in mcp_tools:
            if tool.metadata and 'mcp_server_name' in tool.metadata:
                tool_to_node_lookup[tool.name] = tool.metadata["node_id"]

    return tools, tool_to_node_lookup
```

## AFTER

```python
async def get_attached_llm_tools(
    llm_node_spec: NodeSpec,
    all_nodes: Dict[str, NodeSpec],
) -> Tuple[List[ToolSpec], Dict[str, str], Dict[str, Dict], Dict[str, Dict]]:

    tools: List[ToolSpec] = []
    tool_to_node_lookup: Dict[str, str] = {}
    mcp_configs: Dict[str, Dict] = {}
    mcp_runtime_info: Dict[str, Dict] = {}

    attached_builtin_tools = llm_node_spec.data.get("tools", {}).get("attached", [])
    attached_mcp_servers = llm_node_spec.data.get("mcpServers", {}).get("attached", [])

    _process_builtin_tools(attached_builtin_tools, all_nodes, tools, tool_to_node_lookup)

    if attached_mcp_servers:
        for mcp_node_id in attached_mcp_servers:
            mcp_node_spec = all_nodes[mcp_node_id]
            url = mcp_node_spec.data.get("url")
            transport = "sse" if mcp_node_spec.data.get("transport") == "SSE" else "http"
            tool_prefix = mcp_node_spec.data.get("prefix")

            # MCP client connection config only — no extra keys
            mcp_configs[tool_prefix] = {
                "url": url,
                "transport": transport,
                "httpx_client_factory": _custom_httpx_factory,
                "node_id": mcp_node_id,
            }

            # Runtime metadata kept separate so prefix doesn't leak into client config
            mcp_runtime_info[tool_prefix] = {
                "node_id": mcp_node_id,
                "prefix": tool_prefix,
            }

        client = CallbackMultiserverMCPClient(connections=mcp_configs, tool_name_prefix=True)
        mcp_tools = await client.get_tools()
        tools.extend(mcp_tools)

        for tool in mcp_tools:
            if tool.metadata and "mcp_server_name" in tool.metadata:
                tool_to_node_lookup[tool.name] = tool.metadata["node_id"]

    return tools, tool_to_node_lookup, mcp_configs, mcp_runtime_info
```

**Note on `CallbackMultiserverMCPClient`:** The subclass's `__init__` pops `node_id` from each connection config before calling `super().__init__()`. That still works correctly here because `mcp_configs` entries still contain `node_id`. The pop is destructive on the dict you pass in, so the runtime helper in `tool_llm.py` uses `copy.deepcopy` to protect the caller's dict.

---

# File 2: `graph_builder/compiler.py`

## BEFORE — `_build_nodes` (the `agent.codeless` branch)

```python
if spec.kind == "agent.codeless":
    tool_list, tool_to_node_lookup = await get_attached_llm_tools(spec, nodes)
    fn = await builder(spec, nodes, package, tool_list=tool_list)
    graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
    await self._build_llm_tools_node(graph, spec, nodes, package, tool_list=tool_list, tool_to_node_lookup=tool_to_node_lookup)
else:
    fn = await builder(spec, nodes, package)
    graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
```

## AFTER — `_build_nodes`

```python
if spec.kind == "agent.codeless":
    tool_list, tool_to_node_lookup, mcp_configs, mcp_runtime_info = await get_attached_llm_tools(spec, nodes)
    fn = await builder(spec, nodes, package, tool_list=tool_list)
    graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
    await self._build_llm_tools_node(
        graph,
        spec,
        nodes,
        package,
        tool_list=tool_list,
        tool_to_node_lookup=tool_to_node_lookup,
        mcp_configs=mcp_configs,
        mcp_runtime_info=mcp_runtime_info,
    )
else:
    fn = await builder(spec, nodes, package)
    graph.add_node(nid, fn, metadata={"node_kind": spec.kind, "node_id": spec.id, "node_name": spec.label})
```

## BEFORE — `_build_llm_tools_node`

```python
async def _build_llm_tools_node(
    self,
    graph: StateGraph,
    llm_node: NodeSpec,
    all_nodes: Dict[str, NodeSpec],
    package: OrchestrationPackage,
    tool_list: List = None,
    tool_to_node_lookup: Dict[str, str] = None,
):
    if tool_list:
        tool_node_id = str(uuid.uuid4())
        fn = await build_llm_tool(llm_node, all_nodes, package, tool_list=tool_list, tool_to_node_lookup=tool_to_node_lookup)

        graph.add_node(tool_node_id, fn, metadata={"node_kind": "tool", "node_id": tool_node_id, "node_name": "Tool Node"})
        edge_data = llm_node.data.get("edge_data", [])
        edge_data.append(EdgeData(to=tool_node_id, llm_attached_tool=True))
        llm_node.data["edge_data"] = edge_data
```

## AFTER — `_build_llm_tools_node`

```python
async def _build_llm_tools_node(
    self,
    graph: StateGraph,
    llm_node: NodeSpec,
    all_nodes: Dict[str, NodeSpec],
    package: OrchestrationPackage,
    tool_list: List = None,
    tool_to_node_lookup: Dict[str, str] = None,
    mcp_configs: Dict[str, dict] | None = None,
    mcp_runtime_info: Dict[str, dict] | None = None,
):
    if tool_list:
        tool_node_id = str(uuid.uuid4())
        fn = await build_llm_tool(
            llm_node,
            all_nodes,
            package,
            tool_list=tool_list,
            tool_to_node_lookup=tool_to_node_lookup,
            mcp_configs=mcp_configs,
            mcp_runtime_info=mcp_runtime_info,
        )

        graph.add_node(tool_node_id, fn, metadata={"node_kind": "tool", "node_id": tool_node_id, "node_name": "Tool Node"})
        edge_data = llm_node.data.get("edge_data", [])
        edge_data.append(EdgeData(to=tool_node_id, llm_attached_tool=True))
        llm_node.data["edge_data"] = edge_data
```

---

# File 3: `graph_builder/nodes/tool_llm.py`

This file has the actual behavior change. Four things to update: imports, new helper function, `_build_llm_tool_node` signature and body, and `build_llm_tool` pass-through.

## BEFORE — imports

```python
from __future__ import annotations
import inspect
import json
from typing import Any, Awaitable, Callable, Dict, List
from uuid import uuid4

from clients.http_client import get_http_client_async
from graph_builder.llm_tool_manager import get_attached_llm_tools
from graph_builder.node_wrapper import wrap_node_with_error_policy
from graph_builder.types import NodeSpec
from runtime.models import OrchestrationState
from runtime.output_writer import write_tool_node_event
from runtime.run_context import NodeContext, reset_current_node_ctx, set_current_node_ctx
from langgraph.prebuilt import ToolNode

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from api.v2.models import OrchestrationPackage
from tools.variable_transformer import replace_vars_in_str
```

## AFTER — imports

```python
from __future__ import annotations
import copy
import inspect
import json
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable, Dict, List
from uuid import uuid4

from clients.http_client import get_http_client_async
from graph_builder.llm_tool_manager import CallbackMultiserverMCPClient, get_attached_llm_tools
from graph_builder.node_wrapper import wrap_node_with_error_policy
from graph_builder.types import NodeSpec
from runtime.models import OrchestrationState
from runtime.output_writer import write_tool_node_event
from runtime.run_context import NodeContext, reset_current_node_ctx, set_current_node_ctx
from langgraph.prebuilt import ToolNode

from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.tools import load_mcp_tools
from api.v2.models import OrchestrationPackage
from tools.variable_transformer import replace_vars_in_str
```

Four new imports: `copy`, `AsyncExitStack` from contextlib, `CallbackMultiserverMCPClient` (added alongside the existing `get_attached_llm_tools` import), and `load_mcp_tools`.

## NEW HELPER

Add near the other top-level helpers (e.g. right after `_get_node_context`):

```python
async def _load_runtime_mcp_tools(
    mcp_configs: Dict[str, dict],
    mcp_runtime_info: Dict[str, dict],
):
    """
    Open persistent sessions for all attached MCP servers, load session-bound
    tools, and return (exit_stack, runtime_tools). Caller MUST close the
    exit_stack after ToolNode execution completes.

    AsyncExitStack is required so sessions remain alive across the boundary
    between tool loading and tool invocation in ToolNode.ainvoke(state).
    """
    exit_stack = AsyncExitStack()
    runtime_tools = []

    try:
        # deepcopy because CallbackMultiserverMCPClient.__init__ pops 'node_id'
        # from each connection config, which would mutate the caller's dict.
        client = CallbackMultiserverMCPClient(
            connections=copy.deepcopy(mcp_configs),
            tool_name_prefix=True,
        )

        for server_name in mcp_configs.keys():
            session = await exit_stack.enter_async_context(client.session(server_name))
            tools = await load_mcp_tools(session)

            info = mcp_runtime_info.get(server_name, {})
            prefix = info.get("prefix")
            node_id = info.get("node_id")

            for t in tools:
                if prefix and not t.name.startswith(f"{prefix}_"):
                    t.name = f"{prefix}_{t.name}"

                if t.metadata is None:
                    t.metadata = {}

                t.metadata["mcp_server_name"] = server_name
                t.metadata["node_id"] = node_id

                runtime_tools.append(t)

        return exit_stack, runtime_tools

    except Exception:
        await exit_stack.aclose()
        raise
```

## BEFORE — `_build_llm_tool_node`

```python
def _build_llm_tool_node(
    llm_node_spec: NodeSpec,
    tools: List,
    all_nodes: List[NodeSpec],
    tool_to_node_lookup: Dict[str, str],
    emit_thinking_msg: Callable[[Dict[str, Any], str, str, bool], Awaitable[None]],
):
    # Create a lookup for tool objects by name
    tool_lookup = {tool.name: tool for tool in tools}

    async def node_func(state: OrchestrationState):
        async def awrap(req, handler):
            # Get tool-specific interceptor config or use global config
            tool_name = req.tool_call.get("name")
            tool_node_id = tool_to_node_lookup.get(tool_name)
            tool_node_spec = all_nodes.get(tool_node_id, llm_node_spec)
            tool = tool_lookup.get(tool_name)

            node_ctx = _get_node_context(tool_node_spec)

            if tool_node_spec.kind == "tool":
                req = override_parameters(req, tool_node_spec, node_ctx, tool, state)

            state_for_msg = {
                **state,
                "parameters": req.tool_call.get("args") or {},
                "tool": {"name": tool_name},
            }

            await write_tool_node_event(
                node_id=node_ctx.node_id,
                node_name=node_ctx.node_name,
                stage="start",
                tool_name=tool_name,
                node_kind=tool_node_spec.kind,
            )

            await emit_thinking_msg(
                state_for_msg,
                "before_tool",
                node_ctx.before_execute_status,
                node_ctx.before_execute_status_clear,
            )

            result = handler(req)
            if inspect.isawaitable(result):
                result = await result

            await emit_thinking_msg(
                state_for_msg,
                "after_tool",
                node_ctx.after_execute_status,
                node_ctx.after_execute_status_clear,
            )

            await write_tool_node_event(
                node_id=node_ctx.node_id,
                node_name=node_ctx.node_name,
                stage="end",
                tool_name=tool_name,
                node_kind=tool_node_spec.kind,
            )

            return result

        tool_node = ToolNode(tools, awrap_tool_call=awrap)
        return await tool_node.ainvoke(state)

    return node_func
```

## AFTER — `_build_llm_tool_node`

```python
def _build_llm_tool_node(
    llm_node_spec: NodeSpec,
    tools: List,
    all_nodes: List[NodeSpec],
    tool_to_node_lookup: Dict[str, str],
    emit_thinking_msg: Callable[[Dict[str, Any], str, str, bool], Awaitable[None]],
    mcp_configs: Dict[str, dict] | None = None,
    mcp_runtime_info: Dict[str, dict] | None = None,
):
    # Compile-time tool lookup, used as the default when no MCP rebinding happens
    compile_time_tool_lookup = {tool.name: tool for tool in tools}

    async def node_func(state: OrchestrationState):
        print(f"[MCP FIX] node_func entered, mcp_configs={bool(mcp_configs)}")  # DIAGNOSTIC — remove before PR

        active_tools = tools
        active_tool_lookup = compile_time_tool_lookup
        exit_stack = None

        try:
            if mcp_configs:
                exit_stack, session_tools = await _load_runtime_mcp_tools(
                    mcp_configs,
                    mcp_runtime_info or {},
                )
                print(f"[MCP FIX] loaded {len(session_tools)} session-bound tools")  # DIAGNOSTIC — remove before PR

                builtin_tools = [
                    t for t in tools
                    if not (hasattr(t, "metadata") and t.metadata and "mcp_server_name" in t.metadata)
                ]

                active_tools = builtin_tools + session_tools
                # Rebuild the lookup so awrap sees session-bound tools, not stale compile-time ones.
                # Intentional shadowing of compile_time_tool_lookup for this invocation only.
                active_tool_lookup = {tool.name: tool for tool in active_tools}

            async def awrap(req, handler):
                tool_name = req.tool_call.get("name")
                tool_node_id = tool_to_node_lookup.get(tool_name)
                tool_node_spec = all_nodes.get(tool_node_id, llm_node_spec)
                tool = active_tool_lookup.get(tool_name)

                node_ctx = _get_node_context(tool_node_spec)

                if tool_node_spec.kind == "tool":
                    req = override_parameters(req, tool_node_spec, node_ctx, tool, state)

                state_for_msg = {
                    **state,
                    "parameters": req.tool_call.get("args") or {},
                    "tool": {"name": tool_name},
                }

                await write_tool_node_event(
                    node_id=node_ctx.node_id,
                    node_name=node_ctx.node_name,
                    stage="start",
                    tool_name=tool_name,
                    node_kind=tool_node_spec.kind,
                )

                await emit_thinking_msg(
                    state_for_msg,
                    "before_tool",
                    node_ctx.before_execute_status,
                    node_ctx.before_execute_status_clear,
                )

                result = handler(req)
                if inspect.isawaitable(result):
                    result = await result

                await emit_thinking_msg(
                    state_for_msg,
                    "after_tool",
                    node_ctx.after_execute_status,
                    node_ctx.after_execute_status_clear,
                )

                await write_tool_node_event(
                    node_id=node_ctx.node_id,
                    node_name=node_ctx.node_name,
                    stage="end",
                    tool_name=tool_name,
                    node_kind=tool_node_spec.kind,
                )

                return result

            tool_node = ToolNode(active_tools, awrap_tool_call=awrap)
            return await tool_node.ainvoke(state)

        finally:
            if exit_stack is not None:
                await exit_stack.aclose()

    return node_func
```

**What changed in this function:**

1. Signature gets `mcp_configs` and `mcp_runtime_info` kwargs
2. `tool_lookup` renamed to `compile_time_tool_lookup` (clearer intent)
3. `node_func` wraps `ToolNode` execution in `try/finally` with `exit_stack` lifecycle management
4. `awrap` closes over `active_tool_lookup` instead of the stale compile-time one — moved inside the try block AFTER `active_tool_lookup` is (possibly) rebuilt, so the closure captures the right reference
5. Two `[MCP FIX]` diagnostic prints to prove the code path fires (remove before PR)

## BEFORE — `build_llm_tool`

```python
async def build_llm_tool(
    llm_node_spec: NodeSpec,
    all_nodes: List[NodeSpec],
    package: OrchestrationPackage,
    tool_list: List = None,
    tool_to_node_lookup: Dict[str, str] = None
):
    if tool_list is None:
        tool_list, tool_to_node_lookup = await get_attached_llm_tools(llm_node_spec, all_nodes)

    async def awrap(
        state: OrchestrationState,
        emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
        config: RunnableConfig
    ):
        node_func = _build_llm_tool_node(
            llm_node_spec,
            tool_list,
            all_nodes,
            tool_to_node_lookup,
            emit_thinking_msg,
        )
        return await node_func(state)

    return wrap_node_with_error_policy(llm_node_spec, package, awrap)
```

## AFTER — `build_llm_tool`

```python
async def build_llm_tool(
    llm_node_spec: NodeSpec,
    all_nodes: List[NodeSpec],
    package: OrchestrationPackage,
    tool_list: List = None,
    tool_to_node_lookup: Dict[str, str] = None,
    mcp_configs: Dict[str, dict] | None = None,
    mcp_runtime_info: Dict[str, dict] | None = None,
):
    if tool_list is None:
        tool_list, tool_to_node_lookup, mcp_configs, mcp_runtime_info = await get_attached_llm_tools(llm_node_spec, all_nodes)

    async def awrap(
        state: OrchestrationState,
        emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
        config: RunnableConfig
    ):
        node_func = _build_llm_tool_node(
            llm_node_spec,
            tool_list,
            all_nodes,
            tool_to_node_lookup,
            emit_thinking_msg,
            mcp_configs=mcp_configs,
            mcp_runtime_info=mcp_runtime_info,
        )
        return await node_func(state)

    return wrap_node_with_error_policy(llm_node_spec, package, awrap)
```

The fallback `if tool_list is None` branch now unpacks the 4-tuple from `get_attached_llm_tools`. This branch is used when `build_llm_tool` is called without a pre-fetched tool list.

---

# Validation sequence after applying

1. **Stop the debugger**, clear all breakpoints, restart uvicorn in **normal mode** (not debug). Stale breakpoints and debug-mode pauses can cause Starlette "No response returned" 500 errors that look like the fix isn't firing.
2. **Pre-flight import check** in your venv:
   ```bash
   python -c "from langchain_mcp_adapters.tools import load_mcp_tools; print(load_mcp_tools)"
   python -c "from langchain_mcp_adapters.client import MultiServerMCPClient; print('session' in dir(MultiServerMCPClient))"
   ```
   Second command should print `True`.
3. **Send a test prompt** that exercises an MCP tool call.
4. **Watch the uvicorn console** for both `[MCP FIX]` lines:
   - `[MCP FIX] node_func entered, mcp_configs=True`
   - `[MCP FIX] loaded N session-bound tools`

   If either is missing, the plumbing is broken somewhere between `compiler` → `build_llm_tool` → `_build_llm_tool_node`.
5. **Check Fiddler:** one `initialize` + one `tools/list` + N `tools/call` per turn, instead of the full lifecycle repeated per call.
6. **Remove the two `[MCP FIX]` diagnostic prints** before opening the PR.

---

# Open concern / explicit test gate

**Concurrent MCP tool calls on a shared live session.** If `ToolNode` executes multiple tool calls in parallel within one LLM turn, and those calls share the same live `ClientSession`, we need to verify that the MCP session implementation safely supports that concurrency.

**What to test:** Force one LLM turn to invoke multiple MCP tools against the same server in parallel (e.g. any prompt that triggers 3+ concurrent tool calls to one server). Verify:

- no interleaving / frame corruption
- no JSON-RPC id mismatch
- no deadlocks
- no weird shared-session failures under parallel tool use

**If the test fails:** Keep the same overall design but add a per-server `asyncio.Lock` inside `_load_runtime_mcp_tools` so calls sharing one live session are serialized. This preserves most of the performance win while avoiding contention.

**Note:** In practice, MCP uses JSON-RPC with unique message IDs, so concurrent `call_tool` invocations on one session *should* demultiplex correctly. The risk is implementation quality in the specific session library version, not protocol design. Test before adding the lock preemptively — serializing unnecessarily would cost you some of the win.

---

# Summary of blast radius

| Metric | Value |
|---|---|
| Files changed | 3 |
| Files untouched | `agent_codeless.py` and everything else |
| Approximate lines changed | ~60 across all three files |
| New dependencies | None (`langchain_mcp_adapters.tools.load_mcp_tools` already available) |
| Rollback path | Revert the three files |
| Test updates needed | Any test that unpacks `get_attached_llm_tools` as a 2-tuple — minor |

Rollback is trivial. The patch is localized, reviewable, and preserves every production-critical behavior in `tool_llm.py` except the one targeted choke point: which tool list gets passed into `ToolNode(...)` at execution time.
