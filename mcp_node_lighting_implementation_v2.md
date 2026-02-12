# MCP/Tool Node Lighting Implementation

This document outlines all changes needed to implement node lighting for **ALL tools** (not just MCP servers) in the VAAF system, updated for Jamie's simplified `tool_llm.py` structure.

---

## Overview

**Goals:**
1. ALL tools get node lighting in the UI
2. Only NON-MCP tools go through `override_parameters`
3. MCP tools skip `override_parameters` but still get lighting

**Files to modify:**
1. `runtime/output_writer.py` - Add `write_mcp_node_event` function
2. `graph_builder/nodes/tool_llm.py` - Update `awrap` function
3. `runtime/sse_formatter.py` - Handle the new events

---

## Part 1: `runtime/output_writer.py`

Add this new function after the existing `write_thinking_message` function:

```python
def write_mcp_node_event(
    node_id: str,
    node_name: str,
    stage: str,  # "start" or "end"
    tool_name: str = None,
    node_kind: str = "tool",
):
    """
    Write a node lifecycle event that the SSE formatter will pick up.
    
    This mimics LangGraph's node events so the UI shows nodes lighting up.
    Works for ALL tool types (MCP servers, regular tools, etc.)
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
        "node_kind": node_kind,
        "langgraph_node": node_name,  # KEY: This field makes SSE formatter recognize it
        "langgraph_step": 0,  # Will be set by SSE formatter
        "stage": stage,
        "tool_name": tool_name,
        "thread_id": run_ctx.thread_id if run_ctx else None,
        "run_id": run_ctx.run_id if run_ctx else None,
        "tenant_id": run_ctx.tenant_id if run_ctx else None,
    }

    writer(payload)
```

---

## Part 2: `graph_builder/nodes/tool_llm.py`

### Step 2a: Update imports

At the top of the file, update the import:

```python
from runtime.output_writer import write_thinking_message, write_mcp_node_event
```

### Step 2b: Update `awrap` function

Inside `_build_llm_tool_node`, update the `awrap` function:

```python
async def awrap(req, handler):
    # Get tool-specific interceptor config or use global config
    tool_name = req.tool_call.get("name")
    tool_node_id = tool_to_node_lookup.get(tool_name)
    tool_node_spec = all_nodes.get(tool_node_id, llm_node_spec)
    node_ctx = _get_node_context(tool_node_spec)
    
    # Check if this is an MCP tool
    is_mcp = tool_node_spec.data.get("kind") == "mcpServer"
    
    # Only NON-MCP tools go through override_parameters
    if tool_node_id and not is_mcp:
        req = override_parameters(req, tool_node_spec, node_ctx)
    
    # === ALL TOOLS: Node lighting START ===
    write_mcp_node_event(
        node_id=node_ctx.node_id,
        node_name=node_ctx.node_name,
        stage="start",
        tool_name=tool_name,
        node_kind=tool_node_spec.data.get("kind", "tool"),
    )
    
    # Before execute status (existing Jamie code)
    if node_ctx.report_status and node_ctx.before_execute_status is not None:
        await write_thinking_message(
            node_ctx=node_ctx,
            state={**state, "parameters": req.tool_call["args"], "tool": {"name": tool_name}},
            stage="before_tool",
            message=node_ctx.before_execute_status,
            append=not node_ctx.before_execute_status_clear
        )
    
    # Execute the tool
    result = handler(req)
    if inspect.isawaitable(result):
        result = await result
    
    # After execute status (existing Jamie code)
    if node_ctx.report_status and node_ctx.after_execute_status is not None:
        await write_thinking_message(
            node_ctx=node_ctx,
            state={**state, "parameters": req.tool_call["args"], "tool": {"name": tool_name}},
            stage="after_tool",
            message=node_ctx.after_execute_status,
            append=not node_ctx.after_execute_status_clear
        )
    
    # === ALL TOOLS: Node lighting END ===
    write_mcp_node_event(
        node_id=node_ctx.node_id,
        node_name=node_ctx.node_name,
        stage="end",
        tool_name=tool_name,
        node_kind=tool_node_spec.data.get("kind", "tool"),
    )
    
    return result
```

---

## Part 3: `runtime/sse_formatter.py`

### Step 3a: Handle tuple chunks in `_handle_chain_stream`

The `get_stream_writer()` sends chunks as tuples `('custom', {payload})`. Update the method to handle this:

```python
def _handle_chain_stream(self, event: LangGraphEvent) -> List[str]:
    """
    Handle on_chain_stream events, focusing on:
    - Custom chunks emitted by get_stream_writer (telemetry / thinking).
    - Optional state updates for "updates"/"values" modes.
    """
    data = event.get("data") or {}
    chunk = data.get("chunk")
    
    if chunk is None:
        return []
    
    # === IMPORTANT: Handle tuple format ===
    # get_stream_writer sends ('custom', {actual_payload})
    if isinstance(chunk, tuple) and len(chunk) == 2:
        chunk = chunk[1]  # Extract the actual dict
    
    parts: List[str] = []
    
    # Case 1: custom dict from LangGraph (e.g., get_stream_writer)
    if isinstance(chunk, dict):
        parts = self._handle_custom_dict_chunk(chunk)
    
    # ... rest of existing code ...
    
    return parts
```

### Step 3b: Update `_handle_custom_dict_chunk`

Add handling for `mcp_node_event` type:

```python
def _handle_custom_dict_chunk(self, chunk: Mapping[str, Any]) -> List[str]:
    """Handle dictionary chunks: ignore duplicate messages, emit telemetry or thinking."""
    
    # Ignore dict chunks that contain 'messages' (duplicate of on_chat_model_stream)
    if "messages" in chunk:
        return []
    
    # === ADD THIS: Handle MCP/tool node events ===
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

### Step 3c: Add `_handle_mcp_node_event` method

Add this new method to the `OpenAIResponsesSSEConverter` class:

```python
def _handle_mcp_node_event(self, chunk: Mapping[str, Any]) -> List[str]:
    """Handle MCP/tool node lifecycle events."""
    parts: List[str] = []
    state = self._state
    
    # Get step from completed nodes count (best approximation)
    step = len(state.nodes_completed) + 1
    
    event_type = chunk.get("event")
    node_id = chunk.get("node_id")
    node_name = chunk.get("node_name")
    node_kind = chunk.get("node_kind", "tool")
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
            "node_kind": node_kind,
            "langgraph_node": node_name,
            "langgraph_step": step,
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
            "node_kind": node_kind,
            "langgraph_node": node_name,
            "langgraph_step": step,
            "timestamp": int(time.time()),
        }
        parts.append(_to_sse("response.node.completed", payload))
    
    return parts
```

---

## Summary Table

| File | Changes |
|------|---------|
| `runtime/output_writer.py` | Add `write_mcp_node_event()` function with `node_kind` parameter |
| `graph_builder/nodes/tool_llm.py` | Update import, modify `awrap` to call `write_mcp_node_event` for ALL tools, skip `override_parameters` for MCP only |
| `runtime/sse_formatter.py` | Handle tuple chunks, check for `mcp_node_event` type, add `_handle_mcp_node_event` method |

---

## Testing

1. Run the orchestration with an MCP tool (e.g., jira_mcpServer)
2. Open DevTools → Network → EventStream
3. Look for `response.node.started` and `response.node.completed` events with your tool's `node_id`
4. Verify the node lights up in the UI

---

## Notes

- The `langgraph_step` uses `len(state.nodes_completed) + 1` as an approximation
- The step badge may not show on MCP nodes if the frontend doesn't render it for that `node_kind`
- ALL tools now get lighting, not just MCP servers
- Only NON-MCP tools go through `override_parameters`
