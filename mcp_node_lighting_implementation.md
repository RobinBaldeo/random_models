# MCP Node Lighting Implementation Guide

## Overview
This guide shows how to make MCP server nodes light up in the UI when tools are called, similar to how the Entry and Codeless Agent nodes light up.

---

## Part 1: Create a New Writer Function

**File: `runtime/output_writer.py`**

Add this new function after the existing `write_thinking_message` function:

```python
def write_mcp_node_event(
    node_id: str,
    node_name: str,
    stage: str,  # "start" or "end"
    tool_name: str = None,
    langgraph_step: int = None,
):
    """
    Write an MCP node lifecycle event that the SSE formatter will pick up.
    
    This mimics LangGraph's node events so the UI shows MCP nodes lighting up.
    """
    run_ctx = get_current_run_ctx()
    writer = get_stream_writer()
    
    if writer is None:
        return
    
    # Build payload that mimics LangGraph node events
    payload = {
        "type": "mcp_node_event",
        "event": "on_chain_start" if stage == "start" else "on_chain_end",
        "node_id": node_id,
        "node_name": node_name,
        "node_kind": "mcpServer",
        "langgraph_node": node_name,  # KEY: This field makes SSE formatter recognize it
        "langgraph_step": langgraph_step or 0,
        "stage": stage,
        "tool_name": tool_name,
        "thread_id": run_ctx.thread_id if run_ctx else None,
        "run_id": run_ctx.run_id if run_ctx else None,
        "tenant_id": run_ctx.tenant_id if run_ctx else None,
    }
    
    writer(payload)
```

---

## Part 2: Update tool_llm.py

**File: `graph_builder/nodes/tool_llm.py`**

Update the `override_parameters` function to use the new writer:

```python
from runtime.output_writer import write_thinking_message, write_mcp_node_event

async def override_parameters(
    req: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
    tool_node_spec: NodeSpec,
    nodectx: NodeContext,
    state: OrchestrationState
):
    """
    Generic parameter override with MCP node status reporting.
    """
    nodectx.node_instance_id = uuid4().hex
    token = set_current_node_ctx(nodectx)
    
    try:
        final_args = req.tool_call.get("args")
        
        raw_overrides = tool_node_spec.data.get("parameterOverrides", {}) or {}
        
        for key, value in raw_overrides.items():
            final_args[key] = value
        
        req.tool_call["args"] = final_args
        
        # === MCP NODE START EVENT ===
        # This makes the MCP node light up in the UI
        write_mcp_node_event(
            node_id=nodectx.node_id,
            node_name=nodectx.node_name,
            stage="start",
            tool_name=req.tool_call.get("name"),
        )
        
        # Execute the tool
        result = handler(req)
        
        if inspect.isawaitable(result):
            result = await result
        
        # === MCP NODE END EVENT ===
        # This shows the MCP node completed and transitions back
        write_mcp_node_event(
            node_id=nodectx.node_id,
            node_name=nodectx.node_name,
            stage="end",
            tool_name=req.tool_call.get("name"),
        )
        
        return result
        
    finally:
        nodectx.node_instance_id = None
        reset_current_node_ctx(token)
```

---

## Part 3: Update SSE Formatter to Handle MCP Events

**File: `runtime/sse_formatter.py`**

Add handling for the new MCP node events. In the `_handle_chain_stream` method or create a new handler:

```python
def _handle_mcp_node_event(self, chunk: Mapping[str, Any]) -> List[str]:
    """Handle MCP node lifecycle events."""
    parts: List[str] = []
    state = self._state
    
    event_type = chunk.get("event")
    node_id = chunk.get("node_id")
    node_name = chunk.get("node_name")
    stage = chunk.get("stage")
    
    if not node_id:
        return parts
    
    if stage == "start":
        # Emit node.started event
        payload = {
            "response_id": state.response_id,
            "tenant_id": state.tenant_id,
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "node_id": node_id,
            "node_name": node_name,
            "node_kind": "mcpServer",
            "langgraph_node": node_name,
            "langgraph_step": chunk.get("langgraph_step"),
            "timestamp": int(time.time()),
        }
        parts.append(_to_sse("response.node.started", payload))
        
        # Also emit transition if we have a current node
        if state.current_node_id and state.current_node_id != node_id:
            transition_payload = {
                "response_id": state.response_id,
                "tenant_id": state.tenant_id,
                "thread_id": state.thread_id,
                "run_id": state.run_id,
                "from_node_id": state.current_node_id,
                "to_node_id": node_id,
                "timestamp": int(time.time()),
            }
            parts.append(_to_sse("response.node.transition", transition_payload))
        
        state.current_node_id = node_id
        
    elif stage == "end":
        # Emit node.completed event
        payload = {
            "response_id": state.response_id,
            "tenant_id": state.tenant_id,
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "node_id": node_id,
            "node_name": node_name,
            "node_kind": "mcpServer",
            "langgraph_node": node_name,
            "langgraph_step": chunk.get("langgraph_step"),
            "timestamp": int(time.time()),
        }
        parts.append(_to_sse("response.node.completed", payload))
    
    return parts
```

Then update `_handle_custom_dict_chunk` to call this handler:

```python
def _handle_custom_dict_chunk(self, chunk: Mapping[str, Any]) -> List[str]:
    """Handle dictionary chunks: ignore duplicate messages, emit telemetry or thinking."""
    # Ignore dict chunks that contain 'messages' (duplicate of on_chat_model_stream)
    if "messages" in chunk:
        return []
    
    # === ADD THIS: Handle MCP node events ===
    if chunk.get("type") == "mcp_node_event":
        return self._handle_mcp_node_event(chunk)
    
    # Telemetry path
    if self._is_telemetry_chunk(chunk):
        sse = self._build_telemetry_delta_from_chunk(chunk)
        return [sse] if sse else []

    # Thinking path
    if self._is_thinking_chunk(chunk):
        return [self._build_thinking_delta_from_chunk(chunk)]

    return []
```

---

## Part 4: Ensure tool_to_node_lookup is Populated

**File: `graph_builder/llm_tool_manager.py`**

Make sure MCP tools are added to the lookup (you already did this):

```python
# After getting MCP tools
for tool in mcp_tools:
    if tool.metadata and 'mcp_server_name' in tool.metadata:
        tool_to_node_lookup[tool.name] = tool.metadata['mcp_server_name']

tools.extend(mcp_tools)
```

---

## Part 5: Update awrap to Handle Tool Name Matching

**File: `graph_builder/nodes/tool_llm.py`**

In the `awrap` function inside `_build_llm_tool_node`:

```python
async def awrap(req, handler):
    tool_name = req.tool_call.get("name")
    
    # Try direct lookup first (for builtin tools)
    tool_node_id = tool_to_node_lookup.get(tool_name)
    
    # If not found, search for MCP tools with guid prefix
    if not tool_node_id:
        for full_name, node_id in tool_to_node_lookup.items():
            if "_" in full_name and full_name.endswith("_" + tool_name):
                tool_node_id = node_id
                break
    
    if tool_node_id:
        tool_node_spec = all_nodes[tool_node_id]
        return await override_parameters(
            req, 
            handler, 
            tool_node_spec, 
            _get_node_context(tool_node_spec), 
            state
        )
    
    # Fallback: just call the handler directly
    result = handler(req)
    if inspect.isawaitable(result):
        result = await result
    return result
```

---

## Summary of Changes

| File | What to Add |
|------|-------------|
| `runtime/output_writer.py` | New `write_mcp_node_event()` function |
| `graph_builder/nodes/tool_llm.py` | Call `write_mcp_node_event()` in `override_parameters` |
| `runtime/sse_formatter.py` | New `_handle_mcp_node_event()` method + update `_handle_custom_dict_chunk()` |
| `graph_builder/llm_tool_manager.py` | Already done - populates `tool_to_node_lookup` |

---

## Testing

1. Run the orchestration
2. Open browser dev tools → Network → find the `stream` request → EventStream tab
3. When an MCP tool is called, you should see:
   - `response.node.transition` (from Codeless Agent to MCP)
   - `response.node.started` (MCP node)
   - `response.node.completed` (MCP node)
   - `response.node.transition` (back to Codeless Agent)

4. The UI should light up the MCP node during tool execution

---

## If It Still Doesn't Work

Check these things:

1. Is `write_mcp_node_event` being called? Add a print statement.
2. Is the writer returning None? Check `get_stream_writer()`.
3. Is the SSE formatter receiving the event? Add a print in `_handle_custom_dict_chunk`.
4. Is the event appearing in the EventStream? Check browser dev tools.
5. Does the frontend handle `response.node.started` for `mcpServer` kind nodes?

---



If this approach doesn't work

1. "Does the frontend UI listen for `response.node.started` events for mcpServer nodes, or only for specific node kinds?"
2. "Is there a different event type I should emit for MCP tool transitions?"
3. "Should I be modifying the graph_runner instead of the SSE formatter?"
