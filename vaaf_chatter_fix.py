# =============================================================================
# VAAF MCP Chatter Fix — Changes for agent_codeless.py
# =============================================================================
#
# ONLY ONE FILE CHANGES: agent_codeless.py
# Two changes total:
#   1. Add 2 imports at the top of the file
#   2. Replace the awrap() function inside build_agent_codeless
#
# DO NOT TOUCH:
#   - llm_tool_manager.py
#   - compiler.py
#   - node_wrapper.py
#   - tool_llm.py
#   - Any test files
#   - Any other file
#
# =============================================================================


# =============================================================================
# CHANGE 1: ADD THESE IMPORTS AT THE TOP OF agent_codeless.py
# =============================================================================
#
# Find the existing imports near the top of the file (around lines 1-28).
# Add these two lines anywhere in the import block:

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


# =============================================================================
# CHANGE 2: REPLACE THE awrap() FUNCTION INSIDE build_agent_codeless
# =============================================================================
#
# Location: agent_codeless.py
# Function: build_agent_codeless (around line 473)
# Inner function to replace: awrap (around line 507)
#
# The current awrap() looks like this (DO NOT KEEP — this is for reference):
#
#     async def awrap(
#         state: OrchestrationState,
#         emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
#         config: RunnableConfig,
#     ):
#         all_tools = tool_list
#
#         await emit_thinking_msg(state, "before_llm", node_spec.beforeExecuteStatus, node_spec.beforeExecuteStatusClearPrevious)
#
#         tachyon_auth_creds, apigee_auth_creds = await _get_auth_credentials_from_state(state, model)
#
#         client_with_auth = get_openai_async_client(
#             options,
#             tachyon_auth_creds=tachyon_auth_creds,
#             apigee_auth_creds=apigee_auth_creds
#         )
#
#         bound_client = _bind_client_with_tools(client_with_auth, all_tools, structured_output_schema)
#
#         result = await agent_codeless_execute(
#             bound_client,
#             state,
#             data,
#             queue_request=queue_request,
#         )
#
#         await emit_thinking_msg(state, "after_llm", node_spec.afterExecuteStatus, node_spec.afterExecuteStatusClearPrevious)
#
#         return result
#
# REPLACE IT WITH THIS NEW VERSION:


async def awrap(
    state: OrchestrationState,
    emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
    config: RunnableConfig,
):
    await emit_thinking_msg(
        state,
        "before_llm",
        node_spec.beforeExecuteStatus,
        node_spec.beforeExecuteStatusClearPrevious,
    )

    # Extract auth credentials (including any nested Apigee profile) from state
    tachyon_auth_creds, apigee_auth_creds = await _get_auth_credentials_from_state(
        state, model
    )

    # Create OpenAI client with retrieved auth credentials
    client_with_auth = get_openai_async_client(
        options,
        tachyon_auth_creds=tachyon_auth_creds,
        apigee_auth_creds=apigee_auth_creds,
    )

    # -------------------------------------------------------------------------
    # MCP CHATTER FIX
    # -------------------------------------------------------------------------
    # Build MCP server configs from node_spec at runtime so we can open
    # a persistent session for THIS execution only.
    #
    # Why: MultiServerMCPClient.get_tools() (used by llm_tool_manager at
    # compile time) returns stateless tools that open a NEW MCP session
    # on every .ainvoke() call. That causes excessive HTTP chatter:
    # initialize -> tools/list -> tools/call -> teardown per tool call.
    #
    # By opening one MultiServerMCPClient.session() block here, all tool
    # calls within this awrap() execution share the same session.
    # Reference: GitHub issues #178 and #207, LangChain docs recommend
    # client.session() for stateful MCP usage.
    # -------------------------------------------------------------------------
    mcp_configs = {}
    attached_mcp_servers = node_spec.data.get("mcpServers", {}).get("attached", [])
    for mcp_node_id in attached_mcp_servers:
        mcp_node_spec = all_nodes[mcp_node_id]
        url = mcp_node_spec.data.get("url")
        transport = "sse" if mcp_node_spec.data.get("transport") == "SSE" else "streamable_http"
        tool_prefix = mcp_node_spec.data.get("prefix")
        mcp_configs[tool_prefix] = {
            "url": url,
            "transport": transport,
            "httpx_client_factory": _custom_httpx_factory,
        }

    if mcp_configs:
        # Open ONE persistent MCP session for this execution
        mcp_client = MultiServerMCPClient(connections=mcp_configs)
        async with mcp_client.session() as session:
            # Load MCP tools wired to the persistent session
            mcp_tools = await load_mcp_tools(session)

            # Combine pre-fetched builtin tools with the persistent MCP tools
            # tool_list (from compile time) contains both builtin AND chatty MCP tools.
            # We want to keep the builtin ones but replace the MCP ones with persistent versions.
            # Simple approach: filter out anything from tool_list that came from MCP.
            builtin_tools = [
                t for t in (tool_list or [])
                if not (hasattr(t, "metadata") and t.metadata and "mcp_server_name" in t.metadata)
            ]
            all_tools = builtin_tools + mcp_tools

            bound_client = _bind_client_with_tools(
                client_with_auth, all_tools, structured_output_schema
            )

            result = await agent_codeless_execute(
                bound_client,
                state,
                data,
                queue_request=queue_request,
            )
        # Session auto-closes here when async with exits
    else:
        # No MCP servers attached — use the pre-fetched tool_list as-is
        bound_client = _bind_client_with_tools(
            client_with_auth, tool_list, structured_output_schema
        )

        result = await agent_codeless_execute(
            bound_client,
            state,
            data,
            queue_request=queue_request,
        )

    await emit_thinking_msg(
        state,
        "after_llm",
        node_spec.afterExecuteStatus,
        node_spec.afterExecuteStatusClearPrevious,
    )

    return result


# =============================================================================
# IMPORTANT NOTES
# =============================================================================
#
# 1. The _custom_httpx_factory reference inside awrap() comes from
#    llm_tool_manager.py. You may need to import it at the top of
#    agent_codeless.py:
#
#       from graph_builder.llm_tool_manager import _custom_httpx_factory
#
#    OR define a local equivalent in agent_codeless.py:
#
#       from clients.http_client import get_http_client_async
#
#       def _custom_httpx_factory(headers=None, timeout=None, auth=None):
#           return get_http_client_async(headers=headers, timeout=timeout)
#
# 2. The block at lines 502-505 that calls get_attached_llm_tools STAYS THE SAME.
#    Don't remove it. tool_list is still pre-fetched at compile time.
#    The new awrap() just filters out the chatty MCP tools and replaces them
#    with persistent ones at runtime.
#
# 3. The "return wrap_node_with_error_policy(node_spec, package, awrap)" line
#    at the end of build_agent_codeless STAYS THE SAME.
#
# 4. Test by running VAAF locally with Fiddler attached. You should see
#    significantly fewer HTTP tunnels for MCP server calls compared to before.
#
# =============================================================================
