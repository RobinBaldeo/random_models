# Standalone MCP Tool Node — Technical Summary

**Author:** Robin Baldeo
**Date:** April 28, 2026
**Status:** Backend complete and verified · Frontend wiring complete, final integration test pending

---

## Overview

Adds support for invoking a single MCP tool deterministically as a pipeline step in VAAF graphs, distinct from the existing attached mode where MCP tools are bound to an LLM for runtime selection.

### Two modes (existing + new)

| Mode | Trigger | Behavior |
|---|---|---|
| **Attached** (existing) | MCP node connected to an LLM downstream | All MCP server tools are bound to the model. Model picks tool at runtime per request. |
| **Standalone** (new) | MCP node has no LLM downstream | Exactly one pre-selected tool fires deterministically as a graph step. Result stored under a named output variable. |

Mode is determined automatically by graph topology via the existing `recomputeAttachedTools` reducer. Users do not toggle modes manually — they wire the graph and the platform infers the mode.

---

## Backend Changes

Repository: `App-vaaf-orchestration`
Branch: `mcp_standalone`

### Session 1 — Runtime support

| File | Change |
|---|---|
| `src/graph_builder/llm_tool_manager.py` | Refactored: extracted `_build_mcp_server_config` and `_fetch_mcp_tools` private helpers from existing `get_attached_llm_tools`. Added new public `get_standalone_mcp_tool(mcp_node_spec)` that reuses both helpers and returns one `StructuredTool` matching `data.toolName`. |
| `src/graph_builder/nodes/mcp_tool_non_llm.py` | NEW. Mirrors `tool_non_llm.py` structure but receives a pre-fetched tool and stores result under `data.outputVarName` in `state.variables`. |
| `src/graph_builder/compiler.py` | Added `elif spec.kind == "mcpServer"` branch in `_build_nodes` to dispatch standalone MCP nodes to the new builder. |
| `src/graph_builder/nodes/__init__.py` | Registered `"mcpServer": build_mcp_tool_non_llm` in `NODE_BUILDERS`. |

**Smoke test:** `python smoke_test.py 3` → "All requested smoke tests passed."

### Session 2 — Discovery REST endpoint

| File | Change |
|---|---|
| `src/api/v2/models.py` | Appended four Pydantic classes: `McpDiscoverAuth`, `McpDiscoverRequest`, `McpDiscoveredTool`, `McpDiscoverResponse`. |
| `src/api/v2/routes_mcp.py` | NEW. Single route handler `POST /v2/mcp/discover`. ~30 lines. Reuses `_build_mcp_server_config` and `_fetch_mcp_tools` from Session 1. |
| `src/main.py` | Added `from api.v2.routes_mcp import router as mcp_router` and `app.include_router(mcp_router)` alongside existing v2 routers. |

### Endpoint contract

**Request:**
```json
{
  "url": "https://...mcp",
  "transport": "HTTP",
  "auth": { "type": "None" },
  "tenantId": "..."
}
```

**Response (success):**
```json
{
  "ok": true,
  "tools": [
    {
      "name": "get_page_comments",
      "description": "...",
      "argsSchema": { ... }
    }
  ]
}
```

**Response (connection error):** Returns `200` with `ok: false` and `error: "..."` for inline UI display. Does NOT return 5xx for upstream MCP server failures — the endpoint itself succeeded; the upstream did not.

### Verification

Tested via Swagger UI (`localhost:8000/docs`) against the real Wells Fargo Confluence MCP server in dev:

```
URL: https://itv-mcp-vaaf-dev.apps.str13.ocp.nonprod.wellsfargo.net/confluencetools/mcp
```

Returned 200 with real Confluence tools (`get_page_comments`, `get_page_labels`, etc.) including correct `argsSchema` for each. Backend ships independently.

---

## Frontend Changes

Repository: `App-vaaf-studio-ui`
Branch: `mcp_standalone`

### File-by-file changelog

| File | Status | Change |
|---|---|---|
| `client/src/modules/agent-builder/ir.ts` | Modified | Added optional fields to `MCPServerNode.data`: `toolName?: string`, `parameterOverrides?: Record<string, unknown>`, `outputVarName?: string`. |
| `client/src/modules/agent-builder/types/mcp.ts` | NEW | Defines `McpClient` interface and types `McpToolDefinition`, `McpDiscoverQuery`, `McpDiscoverResult`. |
| `client/src/modules/agent-builder/clients/defaultMcpClient.ts` | NEW | `createDefaultMcpClient(baseUrl="")` factory. Fetch-based client posting to `${baseUrl}/api/v2/mcp/discover`. Returns `{ok, tools, error}`. |
| `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts` | NEW | Hook returning `{tools, isLoading, error, refetch, reset}`. Reads `mcpClient` from `useAgentStudio()` context. |
| `client/src/modules/agent-builder/providers/AgentStudioProvider.tsx` | Modified | Extended `AgentStudioContextValue` with optional `mcpClient: McpClient`. Provider accepts `mcpClient` prop and includes it in the memoized context value. |
| `client/src/modules/agent-builder/components/MCPServerInspector.tsx` | Modified | Added Parameters tab (now 6 tabs total). Added Discover Tools button + dropdown UI in Basics tab. Added output variable name field with copy button. Mode banner indicating standalone vs attached. |
| `client/src/modules/tenant-home/TenantHomePage.tsx` | Modified | Added `createDefaultMcpClient` import (line 31) and `McpClient` type import (line 32). Constructed `const mcpClient = createDefaultMcpClient()` inside `MainContent`. Passed `mcpClient={mcpClient}` to `<AgentStudioProvider>` at the production mount point. |

### Note on AgentBuilderPage.tsx

`client/src/modules/agent-builder/AgentBuilderPage.tsx` was initially modified during development (default export with its own `<AgentStudioProvider>` mount), but this file turned out to be unreferenced by the production routing — `TenantHomePage.tsx` is the actual mount point used by the agent-builder route. The edits in `AgentBuilderPage.tsx` are functionally inert. They could be removed in cleanup, or kept if there's an intent to use this file later.

### Key design decisions

1. **Discovery dropdown UX** matches the Claude Desktop / Cursor MCP pattern — user clicks "Discover Tools," sees a list, picks one. Avoids forcing users to type tool names from memory.

2. **Parameter overrides as JSON textarea for v1.** Schema-driven form generation deferred. JSON.stringify of the parsed object is currently used as a `useEffect` dependency — works but not strictly idiomatic React. Could swap for `useRef`-based approach if linter is strict.

3. **`llmAttached` is automatic, not a user toggle.** Driven by graph topology. Users wire the graph; the platform infers the mode. This avoids a "mode toggle" UI that would conflict with the visual indication of the wiring.

4. **Discovery requires standalone mode to be useful.** In attached mode, the LLM gets all tools and picks per-request — there's nothing for the user to choose. The inspector shows a mode banner accordingly.

5. **No new package dependencies.** Backend uses existing `langchain-mcp-adapters`. Frontend uses native `fetch`.

---

## Open Items

### Final integration test

The Discover Tools button click handler fires correctly and reaches the hook layer. The `mcpClient` prop is passed correctly from `TenantHomePage.tsx`. Build/cache instability during late-day testing prevented final end-to-end confirmation that the dropdown populates with tools. Plan: clean restart in the morning (`rm -rf node_modules/.cache && npm install && npm run dev`) and re-test.

If the dropdown does not populate after clean restart, debug path is:
1. Browser Network tab → look for `POST /api/v2/mcp/discover`
2. If request appears with non-200, check response body for error
3. If no request appears, set breakpoint inside `useMcpToolCatalog.refetch` and inspect `mcpClient` in scope

### Cleanup before PR

- Remove dead-code default export in `AgentBuilderPage.tsx`, OR repurpose if there's a use case
- Add unit tests for new files (`defaultMcpClient.ts`, `useMcpToolCatalog.ts`, `mcp_tool_non_llm.py`, `routes_mcp.py`)
- Consider migration story for existing graphs with `mcpServer` nodes that pre-date the new optional fields (should default cleanly since fields are optional)

### Deferred (out of scope for v1)

- Schema-driven parameter form (replaces JSON textarea)
- "Attached but restricted" mode (expose only specific tools to the LLM, not all)
- Discovery caching (currently fetches fresh on every Discover Tools click)

---

## Files To Review

If reviewing in order of architectural impact:

**Backend:**
1. `src/graph_builder/llm_tool_manager.py` — refactor + new function
2. `src/graph_builder/nodes/mcp_tool_non_llm.py` — new node builder
3. `src/api/v2/routes_mcp.py` — new endpoint
4. `src/graph_builder/compiler.py` — dispatch branch

**Frontend:**
1. `client/src/modules/agent-builder/types/mcp.ts` — interface contract
2. `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts` — hook logic
3. `client/src/modules/agent-builder/components/MCPServerInspector.tsx` — UI surface
4. `client/src/modules/tenant-home/TenantHomePage.tsx` — provider mount diff
