async def _load_runtime_mcp_tools(mcp_configs: Dict[str, dict]):
    """
    Open persistent MCP sessions for all attached servers, load session-bound
    tools, and preserve prefix/metadata for node attribution.

    The AsyncExitStack keeps sessions alive across the caller's ToolNode.ainvoke()
    call — caller MUST await exit_stack.aclose() after tool execution completes.

    Returns:
        exit_stack: AsyncExitStack managing all open sessions.
        runtime_tools: List of LangChain tools bound to persistent sessions.
    """
    exit_stack = AsyncExitStack()
    runtime_tools: List = []

    try:
        # deepcopy because CallbackMultiserverMCPClient.__init__ pops node_id
        client = CallbackMultiserverMCPClient(
            connections=copy.deepcopy(mcp_configs),
            tool_name_prefix=True,
        )

        # Open one persistent session per attached MCP server (multi-server safe)
        for server_name, cfg in mcp_configs.items():
            session = await exit_stack.enter_async_context(client.session(server_name))
            tools = await load_mcp_tools(session)

            # Preserve prefix and metadata so node attribution and telemetry still work
            for t in tools:
                prefix = cfg.get("prefix")
                if prefix and not t.name.startswith(f"{prefix}_"):
                    t.name = f"{prefix}_{t.name}"

                if t.metadata is None:
                    t.metadata = {}

                t.metadata["mcp_server_name"] = server_name
                t.metadata["node_id"] = cfg.get("node_id")

                runtime_tools.append(t)

        return exit_stack, runtime_tools

    except Exception:
        # Clean up any partially-opened sessions on failure
        await exit_stack.aclose()
        raise
		


def _build_llm_tool_node(
    llm_node_spec: NodeSpec,
    tools: List,
    all_nodes: List[NodeSpec],
    tool_to_node_lookup: Dict[str, str],
    emit_thinking_msg: Callable[[Dict[str, Any], str, str, bool], Awaitable[None]],
    mcp_configs: Dict[str, dict] = None,
):


async def node_func(state: OrchestrationState):
	    print(f"[MCP FIX] node_func entered, mcp_configs: {list(mcp_configs.keys()) if mcp_configs else 'EMPTY/NONE'}")

        # MCP CHATTER FIX: open persistent MCP sessions for this tool-node invocation.
        active_tools = tools
        active_tool_lookup = tool_lookup
        exit_stack = None

        try:
            if mcp_configs:
                print(f"[MCP FIX] Opening persistent sessions for {list(mcp_configs.keys())}")
                exit_stack, session_tools = await _load_runtime_mcp_tools(mcp_configs)

                builtin_tools = [
                    t for t in tools
                    if not (hasattr(t, "metadata") and t.metadata and "mcp_server_name" in t.metadata)
                ]

                active_tools = builtin_tools + session_tools
                active_tool_lookup = {t.name: t for t in active_tools}
                print(f"[MCP FIX] Active tool count: {len(active_tools)}, session tool count: {len(session_tools)}")

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


