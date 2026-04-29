# Standalone MCP Tool Node — End of Day Handoff

**Date:** April 28, 2026 (Tuesday evening)
**Status:** Backend ✅ shipped, Frontend 🟡 wiring needs final debug

---

## TL;DR

Backend is complete and verified end-to-end against the real Wells Fargo Confluence MCP server in dev. Frontend UI renders correctly with new tabs, button, and fields, but clicking "Discover Tools" still shows "MCP client not configured" in the inspector. The root cause was identified tonight: `TenantHomePage.tsx` is the actual mount point for the production route, NOT `AgentBuilderPage.tsx` (which is dead code). The fix was applied but isn't taking effect — likely a build/cache issue.

---

## Backend — DONE ✅

All in `App-vaaf-orchestration` on branch `mcp_standalone`.

### Session 1 (runtime support) — committed
- `graph_builder/llm_tool_manager.py` — extracted `_build_mcp_server_config` and `_fetch_mcp_tools` helpers; added new public `get_standalone_mcp_tool(mcp_node_spec)` function
- `graph_builder/nodes/mcp_tool_non_llm.py` — NEW file, mirrors `tool_non_llm.py` but tool is pre-fetched and result stored under `data.outputVarName` in `state.variables`
- `graph_builder/compiler.py` — added `elif spec.kind == "mcpServer"` branch in `_build_nodes`
- `graph_builder/nodes/__init__.py` — registered `"mcpServer": build_mcp_tool_non_llm` in NODE_BUILDERS
- Smoke test (`python smoke_test.py 3`) passes

### Session 2 (discovery REST endpoint) — committed
- `api/v2/models.py` — appended Pydantic classes: `McpDiscoverAuth`, `McpDiscoverRequest`, `McpDiscoveredTool`, `McpDiscoverResponse`
- `src/api/v2/routes_mcp.py` — NEW file, single endpoint `POST /v2/mcp/discover`
- `src/main.py` — added router import and `app.include_router(mcp_router)`

### Verification
Tested via Swagger UI at `localhost:8000/docs` against the real Confluence MCP server:
```
URL: https://itv-mcp-vaaf-dev.apps.str13.ocp.nonprod.wellsfargo.net/confluencetools/mcp
Transport: HTTP
```
Got 200 with real tools (`get_page_comments`, `get_page_labels`, etc.) with proper `argsSchema`. Backend is shippable on its own.

---

## Frontend — Almost done 🟡

All in `App-vaaf-studio-ui` on branch `mcp_standalone`.

### Applied via Co-pilot Opus 4.6 (7 prompts)
- Prompt 1: `ir.ts` — added `toolName?`, `parameterOverrides?`, `outputVarName?` to MCPServerNode.data
- Prompt 2: `types/mcp.ts` — NEW file, McpClient interface
- Prompt 3: `clients/defaultMcpClient.ts` — NEW file, fetch-based client calling `/api/v2/mcp/discover`
- Prompt 4: `hooks/useMcpToolCatalog.ts` — NEW file, returns `{tools, isLoading, error, refetch, reset}`
- Prompt 5: `providers/AgentStudioProvider.tsx` — extended to accept `mcpClient` prop, forwards through context
- Prompt 6: `components/MCPServerInspector.tsx` — added Parameters tab, Discover Tools button, mode banner, output variable name field
- Prompt 7: `AgentBuilderPage.tsx` — added import, constructed mcpClient, added `mcpClient={mcpClient}` prop to `<AgentStudioProvider>`

### UI confirmed rendering
- 6 tabs visible: Basics / Connection / Capabilities / Parameters / Status / Validation
- Discover Tools button visible
- Output variable name field with Copy variable button
- Connection tab populated with Confluence URL, prefix, protocol, transport, auth type

---

## The Bug Tonight

When clicking "Discover Tools":
- Click handler fires (browser console shows `handled discovery clicked <url>`)
- Hook `useMcpToolCatalog` is reachable
- BUT no HTTP request goes out to backend
- Inspector shows red "MCP client not configured"

### Root cause (found tonight)

`AgentBuilderPage.tsx` (the file edited via Prompt 7) is **dead code**. PyCharm's "no usages" annotation confirmed this. The production route is served by `TenantHomePage.tsx` in `client/src/modules/tenant-home/`, which has its OWN `<AgentStudioProvider>` mount that did NOT pass `mcpClient`.

Confirmed via debugger:
- Set breakpoint inside `useMcpToolCatalog.refetch`
- Console eval `mcpClient` → `undefined`
- Provider was mounted but with empty mcpClient prop

### Fix applied tonight (didn't take effect)

In `client/src/modules/tenant-home/TenantHomePage.tsx`:

**Line 31** — added import:
```ts
import {createDefaultMcpClient} from '../agent-builder/clients/defaultMcpClient';
```

**Line 32** — added type import:
```ts
import type {McpClient} from '../agent-builder/types/mcp';
```

**Line 269** — inside `MainContent` function, after `useLoadOrchestrationIr(...)` block:
```ts
const mcpClient :McpClient = createDefaultMcpClient();
```

**Line 311** — added prop to `<AgentStudioProvider>`:
```tsx
<AgentStudioProvider
  key={selectedOrchestration.id}
  tenantId={tenantId}
  toolsClient={mockToolsClient}
  mcpClient={mcpClient}        // ← THIS
  featureFlags={{ showPinnedTools: false }}
>
```

### Why it didn't take effect

Unknown. Possibilities:
1. Dev server crashed silently and isn't actually recompiling
2. Browser is serving cached JS despite hard refresh
3. There's still a TypeScript/build error blocking the bundle
4. The `TenantHomePage.tsx` file isn't actually saved (asterisk on tab)

---

## Tomorrow Morning Plan (15-30 min)

### Step 1 — Clean restart
```bash
# In studio-ui project root
git status                                    # confirm tonight's edits are still there
# Kill any running dev server (Ctrl+C in its terminal)
rm -rf node_modules/.cache                    # nuke build cache
npm install                                   # ensure deps are sane
npm run dev                                   # fresh start
```

Wait for "compiled successfully" or equivalent.

### Step 2 — Browser fresh
- Open `localhost:3000` in a new tab (close any old ones)
- Open dev tools (F12) BEFORE refreshing
- Right-click refresh → "Empty Cache and Hard Reload"
- Navigate to the agent-builder route

### Step 3 — Test
- Click the mcpServer node
- Click "Discover Tools"
- Should see the dropdown populate with Confluence tools

### Step 4 — If it still doesn't work
- F12 → Console tab → look for any RED errors
- F12 → Network tab → click Discover, see if there's a request to `/api/v2/mcp/discover`
- If there IS a request but it's failing, check the response code and message
- If there is NO request, the hook is still bailing — set breakpoint inside `refetch` and check `mcpClient` in scope

### Step 5 — Backup hypothesis
If `mcpClient` is STILL undefined after all this, search for additional `<AgentStudioProvider` mounts that might also be in production code path. Tonight's search found `TenantHomePage.tsx` and `AgentBuilderPage.tsx`. There might be a third somewhere.

---

## Files To Look At Tomorrow

### Edited tonight (verify saved)
- `client/src/modules/tenant-home/TenantHomePage.tsx` — lines 31, 32, 269, 311

### Reference (already correct)
- `client/src/modules/agent-builder/providers/AgentStudioProvider.tsx`
- `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts`
- `client/src/modules/agent-builder/clients/defaultMcpClient.ts`
- `client/src/modules/agent-builder/AgentBuilderPage.tsx` (dead code, can clean up later)

### Backend (don't touch)
- All backend files committed and verified

---

## Design Decisions To Document For PR

When writing the PR description, capture these:

1. **`llmAttached` is automatic, not user-toggled.** Driven by graph topology in the `recomputeAttachedTools` reducer. Users don't think about modes — they think about graph shape. MCP → LLM = attached mode. MCP → next node = standalone mode.

2. **Discovery only meaningful in standalone mode.** In attached mode, the LLM gets all tools and picks at runtime. In standalone mode, user must pick exactly one tool because the node deterministically calls it.

3. **Parameter overrides as JSON textarea for v1.** Schema-driven form deferred. Risk: JSON.stringify(parameterOverridesObj) used as useEffect dependency — works but not strictly idiomatic React. Could swap for useRef-based approach if linter complains.

4. **Discovery endpoint returns 200 with `ok: false` on connection errors** instead of 5xx, for inline UI error display.

5. **Backend reuses helpers from Session 1.** Endpoint is ~30 lines route handler that calls `_build_mcp_server_config` + `_fetch_mcp_tools` from `llm_tool_manager.py`.

6. **No new npm/pip packages added.**

---

## Stretch: Blog Post Material

This work has good material for a Medium post on:
- LangGraph multi-mode tool nodes (LLM-driven vs deterministic)
- Implementing MCP discovery as a REST endpoint that wraps `MultiServerMCPClient`
- React context wiring lessons (the dead-code `AgentBuilderPage` was the entire afternoon's bug)

---

## Honest End-of-Day Note

You worked from morning until 9:30 PM on this. Backend went smoothly. Frontend prompts went smoothly. The last few hours got stuck on a single wiring issue that turned out to be a different file than expected. You found the actual root cause yourself with one well-placed search at 9:10 PM after a walk home.

The remaining work is mechanical, not intellectual. Whatever's blocking the recompile tonight will resolve with a clean `npm install` + restart in the morning. Don't spend more time on it tonight.

Sleep well.
