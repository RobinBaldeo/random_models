# NEW imports at the top
from contextlib import AsyncExitStack
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp import ClientSession
from langchain_mcp_adapters.tools import load_mcp_tools


async def get_attached_llm_tools_persistent(
    llm_node_spec: NodeSpec, 
    all_nodes: Dict[str, NodeSpec]
) -> Tuple[List[ToolSpec], Dict[str, str], AsyncExitStack]:
    """
    Same as get_attached_llm_tools but returns tools wired to persistent
    MCP sessions, plus an AsyncExitStack the caller must close when done.
    """
    tools: List[ToolSpec] = []
    tool_to_node_lookup: Dict[str, str] = {}
    exit_stack = AsyncExitStack()

    # Builtin tools — exact same code as before
    attached_builtin_tools = llm_node_spec.data.get("tools", {}).get("attached", [])
    _process_builtin_tools(attached_builtin_tools, all_nodes, tools, tool_to_node_lookup)

    # MCP tools — persistent session per server
    attached_mcp_servers = llm_node_spec.data.get("mcpServers", {}).get("attached", [])
    
    if attached_mcp_servers:
        for mcp_node_id in attached_mcp_servers:
            mcp_node_spec = all_nodes[mcp_node_id]
            url = mcp_node_spec.data.get("url")
            transport = "sse" if mcp_node_spec.data.get("transport") == "SSE" else "http"
            tool_prefix = mcp_node_spec.data.get('prefix')

            # Open ONE persistent connection per server via exit_stack
            if transport == "sse":
                read, write = await exit_stack.enter_async_context(
                    sse_client(url, httpx_client_factory=_custom_httpx_factory)
                )
            else:
                read, write, _ = await exit_stack.enter_async_context(
                    streamablehttp_client(url, httpx_client_factory=_custom_httpx_factory)
                )

            # Create ONE session, initialize ONCE
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            # Use langchain-mcp-adapters' load_mcp_tools — it converts MCP tools
            # to LangChain StructuredTools using the SAME persistent session
            mcp_tools = await load_mcp_tools(session)

            # Apply prefix and metadata (mirrors CallbackMultiserverMCPClient logic)
            for tool in mcp_tools:
                if tool_prefix:
                    tool.name = f"{tool_prefix}_{tool.name}"
                if tool.metadata is None:
                    tool.metadata = {}
                tool.metadata['mcp_server_name'] = tool_prefix
                tool.metadata['node_id'] = mcp_node_id
                tool_to_node_lookup[tool.name] = mcp_node_id
                tools.append(tool)

    return tools, tool_to_node_lookup, exit_stack
    


# agent_codeless.py
# Around line 502, change from:
if tool_list is None:
    tool_list = await get_attached_llm_tools(node_spec, all_nodes)
    if isinstance(tool_list, tuple):
        tool_list = tool_list[0]

# To:
mcp_exit_stack = None
if tool_list is None:
    tool_list, tool_to_node_lookup, mcp_exit_stack = await get_attached_llm_tools_persistent(
        node_spec, all_nodes
    )
    



#
async def awrap(state, emit_thinking_msg, config):
    all_tools = tool_list
    
    try:
        await emit_thinking_msg(state, "before_llm", ...)
        
        tachyon_auth_creds, apigee_auth_creds = await _get_auth_credentials_from_state(state, model)
        client_with_auth = get_openai_async_client(...)
        bound_client = _bind_client_with_tools(client_with_auth, all_tools, structured_output_schema)
        
        result = await agent_codeless_execute(
            bound_client, state, data, queue_request=queue_request,
        )
        
        await emit_thinking_msg(state, "after_llm", ...)
        return result
    finally:
        # Close persistent MCP sessions when this node finishes
        if mcp_exit_stack is not None:
            await mcp_exit_stack.aclose()