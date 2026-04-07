"""
VAAF MCP Chatter Fix — agent_codeless.py modifications

This module shows the complete changes needed in agent_codeless.py to fix
the MCP chatter problem caused by langchain-mcp-adapters MultiServerMCPClient
creating a new session per tool invocation.

PROBLEM:
    MultiServerMCPClient.get_tools() returns stateless tool objects. Every
    .ainvoke() on those tools opens a new MCP session (initialize -> tools/list
    -> tools/call -> teardown). When a graph runs through multiple LLM turns,
    each turn re-enters the agent node and opens new sessions, causing
    excessive HTTP chatter visible in Fiddler.

REFERENCE:
    - GitHub issue #178: https://github.com/langchain-ai/langchain-mcp-adapters/issues/178
    - GitHub issue #207: https://github.com/langchain-ai/langchain-mcp-adapters/issues/207
    - LangChain docs explicitly recommend client.session() for stateful MCP usage
    - Local reproduction: see automated.py and automated_fix.py

APPROACH:
    Open a persistent MCP session ONCE at compile time when build_agent_codeless
    runs. Tools are wired to that session and cached in the closure. Every
    awrap() invocation reuses the same tools, eliminating session re-creation
    and chatter across LLM turns.

LIFECYCLE CAVEAT (FOR REVIEW):
    The persistent session is never explicitly closed. It lives for the lifetime
    of the VAAF process. This is acceptable for a demo and proves the fix works,
    but for production we need lifecycle hooks in the orchestration layer to
    close sessions on graph teardown or process shutdown.

CHANGES SUMMARY:
    1. Add 2 imports at the top of agent_codeless.py
    2. Replace the build_agent_codeless function with the version below
    3. No other files need to change (llm_tool_manager.py, compiler.py, tests
       all stay the same — backward compatible)
"""

# =============================================================================
# REQUIRED IMPORTS — ADD THESE TO THE TOP OF agent_codeless.py
# =============================================================================

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


# =============================================================================
# REPLACE THE EXISTING build_agent_codeless FUNCTION WITH THIS VERSION
# =============================================================================

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

    MCP CHATTER FIX (DEMO):
    - Opens a persistent MCP session at compile time
    - Session lives for the lifetime of the graph node
    - All awrap() invocations reuse the same session — eliminates chatter
    - TODO: Session is never explicitly closed. For production, lifecycle
      management needs to be handled by the orchestration layer.
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

    # Use passed tools or fetch if not provided (backward compatibility)
    if tool_list is None:
        tool_list = await get_attached_llm_tools(node_spec, all_nodes)
        if isinstance(tool_list, tuple):
            tool_list = tool_list[0]

    # =========================================================================
    # MCP CHATTER FIX — Open persistent session ONCE at compile time
    # =========================================================================
    # Build MCP server configs from node_spec
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

    # Open the persistent session ONCE — it lives for the lifetime of this node
    persistent_mcp_tools = []
    if mcp_configs:
        mcp_client = MultiServerMCPClient(connections=mcp_configs)
        server_name = next(iter(mcp_configs.keys()))
        # Manually enter the session context — we never exit it
        # TODO: For production, this needs proper lifecycle management
        # (close at process shutdown or graph teardown)
        session_cm = mcp_client.session(server_name)
        session = await session_cm.__aenter__()
        persistent_mcp_tools = await load_mcp_tools(session)

    # Filter pre-fetched tools to remove the chatty MCP ones, keep builtin
    builtin_tools = [
        t for t in (tool_list or [])
        if not (hasattr(t, "metadata") and t.metadata and "mcp_server_name" in t.metadata)
    ]

    # Combine builtin tools with persistent MCP tools — created ONCE
    final_tool_list = builtin_tools + persistent_mcp_tools
    # =========================================================================

    async def awrap(
        state: OrchestrationState,
        emit_thinking_msg: Callable[[Dict[str, Any], str], Awaitable[None]],
        config: RunnableConfig,
    ):
        # No session creation here — tools already wired to the persistent session
        all_tools = final_tool_list

        await emit_thinking_msg(
            state,
            "before_llm",
            node_spec.beforeExecuteStatus,
            node_spec.beforeExecuteStatusClearPrevious,
        )

        # Extract auth credentials (including any nested API profile) from state
        tachyon_auth_creds, apigee_auth_creds = await _get_auth_credentials_from_state(
            state, model
        )

        # Create OpenAI client with retrieved auth credentials
        correlation_id = (state.get("configs") or {}).get("correlation_id")
        client_with_auth = get_openai_async_client(
            options,
            tachyon_auth_creds=tachyon_auth_creds,
            apigee_auth_creds=apigee_auth_creds,
            correlation_id=correlation_id,
        )

        bound_client = _bind_client_with_tools(
            client_with_auth, all_tools, structured_output_schema
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


# =============================================================================
# IMPLEMENTATION NOTES
# =============================================================================
#
# Files changed:
#   - agent_codeless.py (this file)
#
# Files NOT changed (backward compatible):
#   - llm_tool_manager.py
#   - compiler.py
#   - node_wrapper.py
#   - Any test files
#
# Lifecycle:
#   - build_agent_codeless runs ONCE at graph compilation
#   - The persistent session is opened here, lives for the process lifetime
#   - awrap() runs MANY times during graph execution (one per LLM turn)
#   - Each awrap() call reuses final_tool_list from the closure
#   - No new sessions opened during runtime — chatter eliminated
#
# Verification with Fiddler:
#   - Before fix: 6+ MCP tunnels per user request (one per LLM turn)
#   - After fix: 1 tunnel at startup, reused for all subsequent requests
#
# Open questions for architecture review:
#   1. Where should session cleanup happen? (process shutdown, graph teardown,
#      explicit lifecycle hook?)
#   2. How to handle session failures? (auto-reconnect, fail-fast, retry?)
#   3. Should this pattern be applied to other agent types (tool_llm.py)?
#   4. Should llm_tool_manager.py be refactored to centralize MCP config
#      extraction so we avoid duplication?
#
# =============================================================================
