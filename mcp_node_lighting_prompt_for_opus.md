# Prompt for Claude Opus 4.5 / GitHub Copilot

## Task: Implement MCP Server Node Lighting in VAAF Agent Builder

I need help implementing a feature that makes MCP (Model Context Protocol) server nodes light up in the UI when their tools are called, similar to how the Entry and Codeless Agent nodes currently light up during execution.

---

## Context and Problem

I'm working on a LangGraph-based orchestration system called VAAF (Wells Fargo Agent Builder). The system has a visual UI that shows a graph of nodes (Entry, Codeless Agent, MCP Servers, Tools, etc.). When a node is executing, it "lights up" with a green border animation.

**Current behavior:**
- Entry node lights up ✓
- Codeless Agent node lights up ✓
- MCP Server nodes do NOT light up ✗

**Desired behavior:**
- When the Codeless Agent calls a tool that belongs to an MCP Server, that MCP Server node should light up
- When the tool completes, the MCP Server node should show completion and transition back to the Codeless Agent

---

## Technical Architecture

### How Node Lighting Currently Works

1. **LangGraph emits events** via `astream_events()` with `stream_mode=["messages", "updates", "custom"]`

2. **GraphRunner** (`runtime/graph_runner.py`) iterates over events and calls `format_sse_event(event)`

3. **SSE Formatter** (`runtime/sse_formatter.py`) converts LangGraph events to Server-Sent Events (SSE)

4. **Key check in SSE Formatter** - `_is_node_event()` method:
```python
def _is_node_event(self, metadata: Dict[str, Any]) -> tuple[bool, str | None]:
    node_id = (metadata or {}).get("node_id")
    is_node = (metadata or {}).get("langgraph_node") is not None and node_id is not None
    return is_node, node_id
```

5. **SSE events that trigger UI lighting:**
   - `response.node.started` - lights up a node
   - `response.node.completed` - shows node finished
   - `response.node.transition` - shows flow between nodes

### The Problem

MCP tool calls happen INSIDE the Codeless Agent node via LangGraph's `ToolNode`. From LangGraph's perspective, it's all one node executing. There's no automatic `on_chain_start` / `on_chain_end` event for MCP nodes because they're not real LangGraph graph nodes.

### What I've Already Done

1. **Built `tool_to_node_lookup`** - Maps tool names to MCP node IDs
   - Location: `graph_builder/llm_tool_manager.py` in `get_attached_llm_tools()`
   - MCP tools have names like `67b2369c-e9e7-4f6a-a81e-e44cf6f16ebd_get_issue`
   - The lookup maps these to their MCP node ID

2. **Created tool interception in `override_parameters`**
   - Location: `graph_builder/nodes/tool_llm.py`
   - When a tool is called, I can intercept it and know which MCP node it belongs to
   - I have access to `nodectx` (NodeContext) with the MCP node's ID, name, etc.

3. **Tried using `write_thinking_message`** but it didn't work
   - The messages were written but didn't appear in the EventStream
   - Reason: The payload didn't have `langgraph_node` field, so SSE formatter ignored it

---

## Files Involved

### 1. `runtime/output_writer.py`
Current `write_thinking_message` function:
```python
def write_thinking_message(
    node_ctx: NodeContext,
    state: Dict[str, Any],
    stage: str,
    message: str,
    append: bool
):
    """Write thinking message to writer"""
    run_ctx = get_current_run_ctx()
    writer = get_stream_writer()
    
    payload = {
        "type": "thinking",
        "stage": stage,
        "node_id": node_ctx.node_id,
        "node_name": node_ctx.node_name,
        "thread_id": run_ctx.thread_id,
        "run_id": run_ctx.run_id,
        "tenant_id": run_ctx.tenant_id,
        "message": replace_vars_in_str(state, message),
        "append": append
    }
    writer(payload)
```

### 2. `graph_builder/nodes/tool_llm.py`
Current structure of `override_parameters`:
```python
async def override_parameters(
    req: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command],
    tool_node_spec: NodeSpec,
    nodectx: NodeContext,
    state: OrchestrationState
):
    """
    Generic parameter override:
    - Supports global overrides (dict) or per-tool overrides (dict keyed by tool name).
    - Applies alias mapping (e.g. searchCount -> count).
    - Only adds overrides that match the tool signature or fit **kwargs.
    - Does not fabricate values for required params except for provided overrides/aliases.
    """
    nodectx.node_instance_id = uuid4().hex
    token = set_current_node_ctx(nodectx)
    
    try:
        final_args = req.tool_call.get("args")
        raw_overrides = tool_node_spec.data.get("parameterOverrides", {}) or {}
        
        for key, value in raw_overrides.items():
            final_args[key] = value
        
        req.tool_call["args"] = final_args
        
        # TODO: Add MCP node start event here
        
        result = handler(req)
        
        if inspect.isawaitable(result):
            result = await result
        
        # TODO: Add MCP node end event here
        
        return result
        
    finally:
        nodectx.node_instance_id = None
        reset_current_node_ctx(token)
```

### 3. `runtime/sse_formatter.py`
Key methods that handle node events:

```python
def _handle_lifecycle_event(self, event, event_type, metadata) -> List[str]:
    """Handle node/tool lifecycle events into structured SSEs."""
    # ... checks for langgraph_node in metadata ...
    # Emits response.node.started and response.node.completed

def _build_node_started_sse(self, event) -> str:
    """Build response.node.started SSE event"""
    # ... builds payload with node_id, node_name, node_kind, etc ...

def _build_node_completed_sse(self, event) -> str:
    """Build response.node.completed SSE event"""
    # ... builds payload ...

def _build_node_transition_sse(self, from_node_id, to_node_id, metadata) -> str:
    """Build response.node.transition SSE event"""
    # ... builds payload ...

def _handle_custom_dict_chunk(self, chunk) -> List[str]:
    """Handle dictionary chunks from custom stream mode"""
    # This is where custom events from get_stream_writer() arrive
    # Currently handles "thinking" and "telemetry" types
```

### 4. `graph_builder/llm_tool_manager.py`
Tool-to-node lookup building:
```python
async def get_attached_llm_tools(llm_node_spec, all_nodes) -> Tuple[List[ToolSpec], Dict[str, str]]:
    tools: List[ToolSpec] = []
    tool_to_node_lookup: Dict[str, str] = {}
    
    # ... builds mcp_configs ...
    
    client = CallbackMultiserverMCPClient(connections=mcp_configs, tool_name_prefix=True)
    mcp_tools = await client.get_tools()
    
    # Map MCP tools to their node IDs
    for tool in mcp_tools:
        if tool.metadata and 'mcp_server_name' in tool.metadata:
            tool_to_node_lookup[tool.name] = tool.metadata['mcp_server_name']
    
    tools.extend(mcp_tools)
    return tools, tool_to_node_lookup
```

### 5. `graph_builder/nodes/agent_codeless.py`
Shows how Codeless Agent uses write_thinking_message (for reference):
```python
nodectx = NodeContext(
    node_id=node_spec.id,
    node_name=node_spec.label,
    report_status=node_spec.reportStatus,
    before_execute_status=node_spec.beforeExecuteStatus,
    before_execute_status_clear=node_spec.beforeExecuteStatusClearPrevious or False,
    after_execute_status=node_spec.afterExecuteStatus,
    after_execute_status_clear=node_spec.afterExecuteStatusClearPrevious or False,
)

# Before LLM call
if nodectx.report_status and nodectx.before_execute_status is not None:
    await write_thinking_message(
        node_ctx=nodectx,
        state=state,
        stage="before_llm",
        message=nodectx.before_execute_status,
        append=not nodectx.before_execute_status_clear
    )
```

---

## Required Implementation

### Step 1: Create New Writer Function

In `runtime/output_writer.py`, add a new function `write_mcp_node_event()` that:
- Takes node_id, node_name, stage ("start" or "end"), tool_name
- Writes to the stream with a payload that includes `langgraph_node` field
- Uses a type that the SSE formatter will recognize (either new type or existing)

### Step 2: Update override_parameters

In `graph_builder/nodes/tool_llm.py`, modify `override_parameters()` to:
- Call `write_mcp_node_event()` with stage="start" BEFORE executing the tool
- Call `write_mcp_node_event()` with stage="end" AFTER the tool completes

### Step 3: Update SSE Formatter

In `runtime/sse_formatter.py`:
- Add a handler method `_handle_mcp_node_event()` that processes MCP node events
- This should emit `response.node.started` when stage is "start"
- This should emit `response.node.completed` when stage is "end"
- Optionally emit `response.node.transition` events
- Update `_handle_custom_dict_chunk()` to call this new handler

---

## Expected SSE Output

When an MCP tool is called, the EventStream should show:

```
event: response.node.transition
data: {"type":"response.node.transition","from_node_id":"<codeless_agent_id>","to_node_id":"<mcp_node_id>",...}

event: response.node.started
data: {"type":"response.node.started","node_id":"<mcp_node_id>","node_name":"mcpServer","node_kind":"mcpServer",...}

event: response.node.completed
data: {"type":"response.node.completed","node_id":"<mcp_node_id>","node_name":"mcpServer","node_kind":"mcpServer",...}

event: response.node.transition
data: {"type":"response.node.transition","from_node_id":"<mcp_node_id>","to_node_id":"<codeless_agent_id>",...}
```

---

## Important Details

1. **NodeContext fields available:**
   - `node_id` - UUID of the MCP server node
   - `node_name` - Label of the node (e.g., "mcpServer")
   - `node_instance_id` - Unique instance ID for this execution
   - `report_status` - Boolean
   - `before_execute_status` / `after_execute_status` - Status message strings

2. **The key field is `langgraph_node`** - The SSE formatter checks for this to determine if an event is a node lifecycle event. Without it, events are ignored.

3. **Stream modes:** The GraphRunner uses `stream_mode=["messages", "updates", "custom"]`. Custom dict payloads written via `get_stream_writer()` come through as `on_chain_stream` events with the dict as the chunk.

4. **Event types the SSE formatter recognizes:**
   - `on_chain_start` / `on_chain_end` - Node lifecycle
   - `on_tool_start` / `on_tool_end` - Tool lifecycle
   - Custom thinking/telemetry chunks

---

## Constraints

- Do not modify the LangGraph graph structure
- Do not modify how nodes are compiled
- Solution should work by emitting custom events through the existing stream
- Must integrate with existing SSE formatter patterns
- Frontend expects specific SSE event types (`response.node.started`, etc.)

---

## Please Provide

1. Complete implementation of `write_mcp_node_event()` function for `output_writer.py`
2. Updated `override_parameters()` function for `tool_llm.py`
3. New `_handle_mcp_node_event()` method for `sse_formatter.py`
4. Updated `_handle_custom_dict_chunk()` method for `sse_formatter.py`
5. Any additional helper functions needed

Make sure the code follows the existing patterns in the codebase and includes proper error handling.
