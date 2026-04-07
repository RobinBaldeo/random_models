# =============================================================================
# Modified build_agent_codeless — MCP Chatter Fix
# =============================================================================
#
# This file contains ONLY the changes needed in agent_codeless.py to fix
# the MCP chatter problem caused by langchain-mcp-adapters MultiServerMCPClient
# creating a new session per tool invocation.
#
# WHAT TO DO:
#   1. Add the new imports at the top of agent_codeless.py
#   2. Replace the existing build_agent_codeless function with the version below
#   3. Do NOT modify llm_tool_manager.py
#   4. Do NOT modify compiler.py
#   5. Do NOT modify any other file
#
# WHY THIS WORKS:
#   - Builtin tools are fetched once at compile time (no session issues)
#   - MCP server configs are captured at compile time
#   - At runtime, awrap() opens a fresh persistent MCP session via
#     client.session() context manager — all tool calls within one awrap()
#     execution share the same session, eliminating the chatter
#   - Session auto-closes when async with exits (no AsyncExitStack needed)
#
# REFERENCE:
#   - LangChain docs recommend client.session() for stateful MCP usage
#   - GitHub issue #207 confirms MultiServerMCPClient is stateless by default
#   - repro_chatty.py vs repro_fixed.py proves the fix locally
# =============================================================================


# -----------------------------------------------------------------------------
# ADD THESE IMPORTS AT THE TOP OF agent_codeless.py
# -----------------------------------------------------------------------------

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from graph_builder.llm_tool_manager import _process_builtin_tools


# -----------------------------------------------------------------------------
# REPLACE THE EXISTING build_agent_codeless FUNCTION WITH THIS VERSION
# -----------------------------------------------------------------------------

async def build_agent_codeless(
    node_spec: NodeSpec,
    all_nodes: Dict[str, NodeSpec],
    package: OrchestrationPackage,
    tool_list: List = None
):
    """
    Stub LLM agent:
    - records system instructions and attached tools
    - returns a placeholder structured output
    Real impl: apply historyPolicy, call LLM, honor structuredOutput, tool use, etc.

    MCP CHATTER FIX:
    - At compile time, only MCP configs and builtin tools are captured.
    - At runtime, awrap() opens a persistent MCP session via client.session()
      so all tool calls within one execution share the same session.
    - This eliminates the per-tool-call initialize/tools/list cycle that
      caused excessive HTTP chatter (visible in Fiddler).
    """

    data = node_spec.data
    model = data.get("model", {})
    queue_request = model.get("queueRequest", False)

    options = OpenAIClientOptions(
        model_id=model.get("modelId"),
        temperature=model.get("temperature"),
        max_tokens=model.get("maxTokens"),
        max_retries=1,
        disable_streaming=model.get("disableStreaming", False)
    )

    structured_output = data.get("structuredOutput")
    structured_output_schema = (
        structured_output.get("schema")
        if structured_output and structured_output.get("enabled")
        else None
    )

    # -------------------------------------------------------------------------
    # Build MCP server config dict from node_spec.
    # Used by awrap() at runtime to open a persistent session per execution.
    # -------------------------------------------------------------------------
    mcp_configs = {}
    attached_mcp_servers = node_spec.data.get("mcpServers", {}).get("attached", [])
    for mcp_node_id in attached_mcp_servers:
        mcp_node_spec = all_nodes[mcp_node_id]
        url = mcp_node_spec.data.get("url")
        transport = "sse" if mcp_node_spec.data.get("transport") == "SSE" else "streamable_http"
        tool_prefix = mcp_node_spec.data.get('prefix')
        mcp_configs[tool_prefix] = {
            "url": url,
            "transport": transport,
            "node_id": mcp_node_id,
        }

    # -------------------------------------------------------------------------
    # Fetch builtin (non-MCP) tools once at compile time.
    # These don't have session issues — they're regular Python tools.
    # -------------------------------------------------------------------------
    builtin_tools = []
    builtin_tool_to_node_lookup = {}
    attached_builtin_tools = node_spec.data.get("tools", {}).get("attached", [])
    _process_builtin_tools(
        attached_builtin_tools,
        all_nodes,
        builtin_tools,
        builtin_tool_to_node_lookup,
    )

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

        # ---------------------------------------------------------------------
        # Open a persistent MCP session for THIS execution.
        # All tool calls within this awrap() reuse the same session,
        # eliminating the chatter caused by stateless tool invocations.
        # ---------------------------------------------------------------------
        if mcp_configs:
            mcp_client = MultiServerMCPClient(connections=mcp_configs)
            async with mcp_client.session() as session:
                # Load MCP tools wired to the persistent session
                mcp_tools = await load_mcp_tools(session)

                # Apply prefixes and metadata
                # (mirrors the original CallbackMultiserverMCPClient behavior)
                for tool in mcp_tools:
                    for prefix in mcp_configs.keys():
                        if tool.name.startswith(prefix):
                            if tool.metadata is None:
                                tool.metadata = {}
                            tool.metadata['mcp_server_name'] = prefix
                            tool.metadata['node_id'] = mcp_configs[prefix]['node_id']
                            break

                all_tools = builtin_tools + mcp_tools

                bound_client = _bind_client_with_tools(
                    client_with_auth,
                    all_tools,
                    structured_output_schema,
                )

                result = await agent_codeless_execute(
                    bound_client,
                    state,
                    data,
                    queue_request=queue_request,
                )
            # Session auto-closes here when `async with` exits
        else:
            # No MCP servers attached — just use builtin tools
            bound_client = _bind_client_with_tools(
                client_with_auth,
                builtin_tools,
                structured_output_schema,
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

    return wrap_node_with_error_policy(node_spec, package, awrap)
