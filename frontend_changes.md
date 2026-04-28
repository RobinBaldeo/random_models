# Frontend Changes — Standalone MCP Tool Node

This document mirrors the backend doc's structure: every UI change shown file-by-file with **original → modified** diffs where applicable, and explicit "added," "modified," and "untouched" callouts.

The frontend is genuinely a bit riskier than the backend was. This doc calls that out honestly per file rather than papering over it.

---

## Honest scope assessment up front

**Three of the seven changes are entirely new files**, so they're not really "diffs" — they're additions. New files are lower-risk than edits to existing files (nothing existing breaks if a new file has a bug), but they require correct *wiring* to be exercised at all.

**Two of the changes are append-only** to existing types/interfaces. Near-zero risk.

**One change is a real edit** to an existing file (`AgentStudioProvider.tsx`) — but it's an additive edit (one new optional field), not a rewrite.

**One change is a substantial rewrite** of an existing file (`MCPServerInspector.tsx`). This is the highest-risk piece. It's also where you'll spend most of your testing time tomorrow.

**One change requires finding a file I don't have visibility into** — wherever `<AgentStudioProvider>` is mounted in your app. This is a one-line wiring step. Until it's done, the **Discover Tools** button shows "MCP client not configured" — nothing else breaks.

---

## File inventory

| # | File | Change type | Risk | Net new lines |
|---|---|---|---|---|
| 1 | `client/src/modules/agent-builder/model/ir.ts` | Append (3 fields on existing interface) | Near-zero | ~12 |
| 2 | `client/src/modules/agent-builder/types/mcp.ts` | NEW file | Low | ~50 |
| 3 | `client/src/modules/agent-builder/clients/defaultMcpClient.ts` | NEW file | Low | ~80 |
| 4 | `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts` | NEW file | Low | ~80 |
| 5 | `client/src/modules/agent-builder/providers/AgentStudioProvider.tsx` | Additive edit (one new optional prop) | Low | ~5 |
| 6 | `client/src/modules/agent-builder/components/MCPServerInspector.tsx` | Substantial rewrite | **Medium** | (full file replacement, ~700 lines) |
| 7 | App root — wherever `<AgentStudioProvider>` is mounted | One-line edit | Low | 1 |

**Total: 3 new files, 3 edited files, 1 wiring edit at app root.**

The substantial rewrite is File 6. Read its section carefully.

---

# File 1 — `client/src/modules/agent-builder/model/ir.ts`

**Append-only.** Three new optional fields on the existing `MCPServerNode.data` shape.

## What stays exactly the same

Every other interface in `ir.ts` — `RouterLLMNode`, `ToolNode`, `CodelessAgentNode`, `IRGraph`, etc. — **untouched**. Existing fields on `MCPServerNode` are also unchanged.

## What changes

The existing `MCPServerNode` interface gains three optional fields:

**Original:**

```ts
export interface MCPServerNode extends BaseNode {
  kind: "mcpServer";
  data: {
    url: string;
    prefix?: string;
    prefixOverridden?: boolean;
    protocol: string;
    transport?: "HTTP" | "SSE";
    auth?: { type: "None" | "OBO" | "client_credentials" | "api_key" | "mtls"; scopes?: string[]; secretRef?: string | null };
    capabilities?: { tools?: boolean; resources?: boolean; prompts?: boolean; sampling?: boolean };
    namespaceFilter?: string[];
    metadata?: Record<string, unknown>;
    llmAttached?: boolean;
  };
}
```

**Modified (three new fields appended at the bottom of `data`):**

```ts
export interface MCPServerNode extends BaseNode {
  kind: "mcpServer";
  data: {
    url: string;
    prefix?: string;
    prefixOverridden?: boolean;
    protocol: string;
    transport?: "HTTP" | "SSE";
    auth?: { type: "None" | "OBO" | "client_credentials" | "api_key" | "mtls"; scopes?: string[]; secretRef?: string | null };
    capabilities?: { tools?: boolean; resources?: boolean; prompts?: boolean; sampling?: boolean };
    namespaceFilter?: string[];
    metadata?: Record<string, unknown>;
    llmAttached?: boolean;

    // === NEW ===
    /** Specific MCP tool this node should invoke when running standalone. */
    toolName?: string;
    /** Parameter overrides passed to the tool at runtime. */
    parameterOverrides?: Record<string, any>;
    /** Variable name to store the tool result under in graph state. */
    outputVarName?: string;
    // === END NEW ===
  };
}
```

## Risk assessment

**Near-zero.** All three fields are optional. Existing graphs that don't have them continue to typecheck and serialize correctly. Existing tests that don't reference them are unaffected.

---

# File 2 — `client/src/modules/agent-builder/types/mcp.ts` (NEW)

New file. Defines the `McpClient` interface and discovery types.

## What it contains

A small file with type aliases and one interface. Mirrors the shape of `types/tools.ts` so the patterns line up.

```ts
export type McpTransport = "HTTP" | "SSE";

export type McpAuthType = "None" | "OBO" | "client_credentials" | "api_key" | "mtls";

export type McpAuthConfig = {
  type: McpAuthType;
  scopes?: string[];
  secretRef?: string | null;
};

export type McpToolDefinition = {
  name: string;
  description?: string;
  argsSchema?: Record<string, any>;
};

export type McpDiscoverQuery = {
  url: string;
  transport?: McpTransport;
  auth?: McpAuthConfig;
  tenantId?: string;
};

export type McpDiscoverResult = {
  ok: boolean;
  tools: McpToolDefinition[];
  error?: string;
};

export interface McpClient {
  discoverTools(query: McpDiscoverQuery): Promise<McpDiscoverResult>;
}
```

## Why a new file

`types/tools.ts` already exists for tool catalog types. Mirroring that pattern (one types file per domain) keeps the codebase organized. Could also live inline in `clients/defaultMcpClient.ts`, but separating types from implementation is the established convention.

## Risk assessment

**Low.** New file, new exports. Nothing imports it yet except the new files in this delivery (Files 3, 4, 5, 6).

---

# File 3 — `client/src/modules/agent-builder/clients/defaultMcpClient.ts` (NEW)

New file. Provides a fetch-based default implementation of the `McpClient` interface.

## What it does

Exports `createDefaultMcpClient(baseUrl?)` factory that returns an `McpClient` object. Internally calls `fetch()` against `/api/v2/mcp/discover` (the backend endpoint from Session 2 of the backend doc).

```ts
import type { McpClient, McpDiscoverQuery, McpDiscoverResult, McpToolDefinition } from "../types/mcp";

export function createDefaultMcpClient(baseUrl: string = ""): McpClient {
  const endpoint = `${baseUrl}/api/v2/mcp/discover`;

  return {
    async discoverTools(query: McpDiscoverQuery): Promise<McpDiscoverResult> {
      const body: Record<string, unknown> = {
        url: query.url,
        transport: query.transport ?? "HTTP",
      };
      if (query.auth) body.auth = query.auth;
      if (query.tenantId) body.tenantId = query.tenantId;

      let res: Response;
      try {
        res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(body),
        });
      } catch (err: any) {
        return { ok: false, tools: [], error: `Network error: ${err?.message ?? "fetch failed"}` };
      }

      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const text = await res.text(); if (text) detail = `${detail}: ${text.slice(0, 200)}`; } catch {}
        return { ok: false, tools: [], error: detail };
      }

      let data: any;
      try { data = await res.json(); }
      catch (err: any) { return { ok: false, tools: [], error: `Could not parse response: ${err?.message ?? "parse error"}` }; }

      const tools: McpToolDefinition[] = Array.isArray(data?.tools)
        ? data.tools.map((t: any): McpToolDefinition => ({
            name: String(t?.name ?? ""),
            description: t?.description ?? undefined,
            argsSchema: t?.argsSchema ?? t?.args_schema ?? undefined,
          }))
        : [];

      return { ok: Boolean(data?.ok), tools, error: data?.error ?? undefined };
    },
  };
}
```

## Honest concern about this file

Your existing `useToolsCatalog.ts` reads `toolsClient` from context — it doesn't call `fetch()` directly. The real `ToolsClient` implementation likely uses a project-specific HTTP wrapper (auth headers, base URL injection, error normalization).

**This file uses raw `fetch()`** because I didn't see your real `ToolsClient` implementation file. That means:

- If your project requires custom auth headers (e.g. a bearer token from session storage), they won't be sent.
- If your API base URL comes from a config helper, this file ignores it (`baseUrl` defaults to "" — relative URLs against the same origin).
- If your real client normalizes errors in a specific way, this file doesn't match the convention.

**For local dev with a stub MCP server, raw `fetch` works fine.** For production/staging, you may want to swap `fetch` for whatever your real `ToolsClient` uses internally.

## Risk assessment

**Low for local dev. Medium for production deployment.** Worst case: in production, the **Discover Tools** button hits the backend without auth and gets a 401. Easy fix once you see it — replace `fetch` with your real HTTP wrapper, signature stays the same.

---

# File 4 — `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts` (NEW)

New file. Hook that reads `mcpClient` from context and exposes discovery state to the inspector.

## What it does

Returns `{ tools, isLoading, error, refetch, reset }`. Mirrors `useToolsCatalog` but with manual refetch (instead of auto-fetch on mount), since hitting a remote MCP server isn't free and the user clicks **Discover Tools** explicitly.

```ts
import { useCallback, useMemo, useState } from "react";
import type { McpDiscoverQuery, McpToolDefinition } from "../types/mcp";
import { useAgentStudio } from "../providers/AgentStudioProvider";

export type UseMcpToolCatalogResult = {
  tools: McpToolDefinition[];
  isLoading: boolean;
  error?: string;
  refetch: (query: McpDiscoverQuery) => Promise<void>;
  reset: () => void;
};

export function useMcpToolCatalog(): UseMcpToolCatalogResult {
  const { tenantId, mcpClient } = useAgentStudio();
  const [tools, setTools] = useState<McpToolDefinition[]>([]);
  const [error, setError] = useState<string | undefined>(undefined);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const refetch = useCallback(async (query: McpDiscoverQuery): Promise<void> => {
    if (!mcpClient) { setError("MCP client not configured"); return; }
    if (!query.url || !query.url.trim()) { setError("Server URL is required"); return; }

    setIsLoading(true);
    setError(undefined);

    try {
      const effectiveQuery: McpDiscoverQuery = { ...query, tenantId: query.tenantId ?? tenantId };
      const res = await mcpClient.discoverTools(effectiveQuery);
      if (res.ok) { setTools(res.tools); setError(undefined); }
      else { setTools([]); setError(res.error ?? "Discovery failed"); }
    } catch (err: any) {
      setTools([]); setError(err?.message ?? String(err));
    } finally {
      setIsLoading(false);
    }
  }, [mcpClient, tenantId]);

  const reset = useCallback((): void => { setTools([]); setError(undefined); setIsLoading(false); }, []);

  return useMemo((): UseMcpToolCatalogResult => ({ tools, isLoading, error, refetch, reset }),
    [tools, isLoading, error, refetch, reset]);
}
```

## Risk assessment

**Low.** Standard React hook pattern. If `mcpClient` is missing from context (because File 7's wiring isn't done), `refetch` sets `error` to "MCP client not configured" and the UI shows that message. Graceful degradation.

---

# File 5 — `client/src/modules/agent-builder/providers/AgentStudioProvider.tsx`

**Additive edit.** Three small changes. Existing context shape and consumers are preserved.

## What stays exactly the same

- The bootstrap effect that loads `toolsIndex` from `toolsClient.listTools({ limit: 1000 })` — **untouched**.
- The existing fields in `AgentStudioContextValue` (`tenantId`, `toolsClient`, `featureFlags`, `toolsIndex`) — **untouched**.
- `useAgentStudio()` hook — **untouched** (other than now returning the additional `mcpClient` field via context).

## What changes

Three small additions:

**Change 5.1 — New import (one line):**

```ts
import type { McpClient } from "../types/mcp"; // NEW
```

**Change 5.2 — Add `mcpClient?: McpClient` to `AgentStudioContextValue`:**

```ts
type AgentStudioContextValue = {
  tenantId: string;
  toolsClient: ToolsClient;
  mcpClient?: McpClient; // NEW — optional so existing test setups don't break
  featureFlags?: FeatureFlags;
  toolsIndex: Record<string, ToolDefinition>;
};
```

**Change 5.3 — Accept `mcpClient` as a prop and forward it through context:**

```ts
export function AgentStudioProvider({
  tenantId,
  toolsClient,
  mcpClient,                 // NEW
  featureFlags,
  children,
}: {
  tenantId: string;
  toolsClient: ToolsClient;
  mcpClient?: McpClient;     // NEW
  featureFlags?: FeatureFlags;
  children: React.ReactNode;
}): JSX.Element {
  // ... existing toolsIndex bootstrap effect unchanged ...

  const value: AgentStudioContextValue = useMemo<AgentStudioContextValue>(
    (): AgentStudioContextValue => ({
      tenantId,
      toolsClient,
      mcpClient,             // NEW
      featureFlags,
      toolsIndex,
    }),
    [tenantId, toolsClient, mcpClient, featureFlags, toolsIndex], // mcpClient added to deps
  );
  return <AgentStudioContext.Provider value={value}>{children}</AgentStudioContext.Provider>;
}
```

## Risk assessment

**Low.** `mcpClient` is optional. Existing tests that don't pass it continue to work. Existing components that consume `useAgentStudio()` continue to get the same fields they did before, plus an optional `mcpClient` they can ignore.

---

# File 6 — `client/src/modules/agent-builder/components/MCPServerInspector.tsx`

**This is the substantial change.** It's a near-full rewrite (~700 lines), but the rewrite preserves all existing behavior while adding standalone-mode UI.

## Honest framing

Rather than show line-by-line diffs of a 700-line file (which would be unreadable), I'll group the changes into seven logical sections. Each section is self-contained — you can review them one at a time.

## What stays semantically the same

- All five existing tabs (Basics, Connection, Capabilities, Status, Validation) render the same fields with the same behavior.
- `update(patch)` helper still dispatches `SET_GRAPH`.
- `labelToPrefix` helper, prefix auto-population effect, validation issue filtering — all preserved.
- Existing test queries (`getByDisplayValue('Server1')`, `getByRole('tab', { name: 'Basics' })`, etc.) continue to find their targets.

## What is added (seven logical changes)

### Change 6.1 — Tab union extended

The `tab` state union now includes `"Parameters"`:

```ts
const [tab, setTab] = useState<
  "Basics" | "Connection" | "Capabilities" | "Parameters" | "Status" | "Validation"
>("Basics");
```

### Change 6.2 — Read `llmAttached` as derived state

Read-only. `recomputeAttachedTools` in the reducer manages this flag automatically based on graph topology.

```ts
const isAttachedToAgent: boolean = Boolean(node?.data?.llmAttached);
```

### Change 6.3 — Tab list is mode-dependent

```ts
const tabList = isAttachedToAgent
  ? ["Basics", "Connection", "Capabilities", "Status", "Validation"]
  : ["Basics", "Connection", "Capabilities", "Parameters", "Status", "Validation"];

const effectiveTab = tabList.includes(tab) ? tab : "Basics";
```

The `<nav>` maps over `tabList` instead of a hardcoded list. All conditional renders use `effectiveTab`.

### Change 6.4 — Discovery hook integration

```ts
const { tools: discoveredTools, isLoading: isDiscovering, error: discoverError, refetch: refetchTools, reset: resetTools } =
  useMcpToolCatalog();

const [hasDiscovered, setHasDiscovered] = useState<boolean>(false);

const handleDiscover = async (): Promise<void> => {
  if (!currentUrl.trim()) return;
  await refetchTools({
    url: currentUrl,
    transport: (node.data?.transport as "HTTP" | "SSE") ?? "HTTP",
    auth: node.data?.auth,
  });
  setHasDiscovered(true);
};

// Reset cached tools when URL changes
useEffect((): void => {
  if (hasDiscovered) { resetTools(); setHasDiscovered(false); }
}, [currentUrl]);
```

### Change 6.5 — Mode banner at top of Basics tab

Read-only. Color-coded. Tells the author which mode they're in.

```tsx
<div style={{
  padding: "0.5rem 0.75rem",
  marginBottom: "0.75rem",
  borderRadius: 6,
  border: "1px solid #e5e7eb",
  background: isAttachedToAgent ? "#eff6ff" : "#f0fdf4",
}}>
  <strong>Mode:</strong>{" "}
  {isAttachedToAgent
    ? <>Attached to agent — tools exposed to the LLM at runtime.</>
    : <>Standalone — node invokes a single MCP tool as a pipeline step.</>}
</div>
```

### Change 6.6 — Standalone-only fields in Basics

Gated by `!isAttachedToAgent`. Three pieces:

- A "Tool" section with the **Discover Tools** button + `<select>` dropdown populated from `discoveredTools`. Shows loading state, error state, "no tools found" empty state, and the selected tool's description.
- An "Output variable name" input with a "Copy variable" button that copies `$.variables.<n>` to clipboard.
- The dropdown intelligently handles the case where a saved `toolName` exists but no fresh discovery has run — it shows the saved name as an option labeled "(not in current catalog)" so users can see what's currently configured.

### Change 6.7 — New Parameters tab

Renders only when `effectiveTab === "Parameters" && !isAttachedToAgent`. Contains:

- A read-only preview of the selected tool's `argsSchema` (so users can see what fields the tool expects).
- A JSON textarea for `parameterOverrides` with a local draft buffer. Users can type freely (including invalid intermediate states); only commits to graph state when the JSON parses cleanly. Inline error message on parse failure.
- A "Reset overrides" button.

## What's NOT in this rewrite

- No changes to the `Connection` tab's field set.
- No changes to the `Capabilities` tab's checkboxes or namespace filter.
- No changes to the `Status` tab.
- No changes to the `Validation` tab.
- No new dispatch action types. Everything still flows through `update(patch)` → `SET_GRAPH`.
- No reducer changes.

## Risk assessment

**Medium.** This is the most code in the whole feature. The rewrite is conservative (additive, not replacing existing behavior), but a 700-line file rewrite is a 700-line file rewrite. Your existing tests should pass because the test queries target stable display values and tab names that haven't changed, but you should run the test suite and watch for regressions.

**The biggest specific risk:** the JSON draft buffer in the Parameters tab uses `JSON.stringify(parameterOverridesObj)` as a `useEffect` dependency. That's not strictly idiomatic React. If your linter is strict about this, you may need to swap it for a `useRef`-based approach. Functional behavior is correct.

**Mitigation:** Apply this file last. If anything breaks, you have working backend + working hook + working types — just the inspector to debug.

---

# File 7 — App root wiring

## What needs to happen

Wherever `<AgentStudioProvider>` is mounted in your app, add a `mcpClient` prop:

```tsx
import { createDefaultMcpClient } from "./modules/agent-builder/clients/defaultMcpClient";

const mcpClient = createDefaultMcpClient(); // or createDefaultMcpClient(API_BASE_URL)

<AgentStudioProvider
  tenantId={tenantId}
  toolsClient={toolsClient}
  mcpClient={mcpClient}    // NEW
>
  {children}
</AgentStudioProvider>
```

## How to find the mount point

In your project root, run:

```bash
grep -r "AgentStudioProvider" client/src --include="*.tsx" --include="*.ts"
```

You'll see imports in test files and consumers (which is `useAgentStudio` calls — ignore those), plus one or two files that have `<AgentStudioProvider ...>` JSX. That JSX is the mount point. Likely candidates: `App.tsx`, `index.tsx`, a route file, or a layout component.

## Honest concern about this file

I cannot tell you exactly which file to edit because I don't have visibility into your app shell. Until you find this mount point and add the one line, the **Discover Tools** button will show "MCP client not configured" — nothing else breaks, but the discovery flow won't work.

This is the **only step in the whole feature that depends on you finding a file I can't see.** It's also the easiest to verify: clicking **Discover Tools** without it shows a clear error message that tells you exactly what's missing.

## Risk assessment

**Low when found.** Adding one prop to an existing JSX element. Doesn't change any existing rendering.

---

# Verification — what to do after applying

Apply in this order. Each step is independently checkable.

## Step 1 — Apply Files 1, 2 (types/interfaces)

Run `npm run typecheck` (or your project's TS check). Should compile.

**If this fails:** the type addition collides with something I didn't see. Show me the error.

## Step 2 — Apply Files 3, 4 (client + hook)

Run typecheck again. Should compile (these import types from File 2, so they need it applied first).

**If this fails:** likely an import path issue. Adjust `../types/mcp` if your project structure differs.

## Step 3 — Apply File 5 (provider extension)

Run typecheck. Should compile. Run existing tests:

```bash
npm test -- AgentStudioProvider
```

Existing tests should pass because `mcpClient` is optional.

**If this fails:** test fixtures may need updating. Show me the error.

## Step 4 — Apply File 7 (app root wiring)

Restart the dev server. Visit the studio UI. Should boot without errors. (Without this step, File 6's discovery button will fail; everything else still works.)

## Step 5 — Apply File 6 (inspector rewrite)

This is the big one. Run typecheck:

```bash
npm run typecheck
```

Run the existing inspector tests:

```bash
npm test -- MCPServerInspector
```

These should pass because the test queries target stable display values that haven't changed.

**If they fail:** likely a label-string mismatch or test fixture issue with `llmAttached`. Show me the error.

## Step 6 — Manual UI test

Drop an `mcpServer` node on the canvas. Verify:

1. Banner shows "Standalone".
2. Go to Connection tab, enter `http://localhost:8123` (your stub server URL).
3. Switch to Basics tab. Click **Discover Tools**.
4. Should see "Discovering…" briefly, then dropdown populates with tools.
5. Pick a tool. Description shows below dropdown.
6. Switch to Parameters tab. Enter `{"message": "hello"}`. Set `outputVarName` to `result`.
7. Save the graph. Run it. Tool fires. `$.variables.result` populated.
8. Drag an edge from a codeless agent to the mcpServer node. Banner flips to "Attached." Discovery UI and Parameters tab disappear.
9. Disconnect. Flips back to Standalone. Values preserved on `node.data` even while hidden.
10. Set URL to a bad value, click Discover. Inline red error shows.

If steps 1-10 all work, the feature is shipped end-to-end.

---

# What is explicitly NOT changing

- **No reducer changes.** `AgentBuilderContext.tsx` is unchanged. Action types unchanged.
- **No canvas changes.** `MCPServerNode.tsx` (the visual on the canvas) is unchanged.
- **No palette changes.** Same `mcpServer` kind, same palette entry.
- **No changes to `useToolsCatalog.ts`.** The existing tools catalog hook is untouched.
- **No changes to `useValidation.ts`.**
- **No changes to `ToolInspector.tsx`.**
- **No new dispatch actions.** Everything routes through existing `SET_GRAPH` / `UPDATE_NODE_LABEL`.
- **No npm packages added.**

---

# Files in this delivery

Saved to `/mnt/user-data/outputs/`:

- `ir_addition.ts` — File 1 contents (the three new fields)
- `types_mcp.ts` — File 2 contents (new types file)
- `defaultMcpClient.ts` — File 3 contents (fetch-based client)
- `useMcpToolCatalog.ts` — File 4 contents (the hook)
- `AgentStudioProvider.tsx` — File 5 contents (modified provider)
- `MCPServerInspector.tsx` — File 6 contents (rewritten inspector)
- `frontend_changes.md` — this document

File 7 has no artifact — it's a one-line edit at your app root.

---

# Where to focus your worry

If you only have time to carefully review one file, **make it File 6 (`MCPServerInspector.tsx`)**. That's where the most code change is and where regressions are most likely to surface.

If you have time for a second careful review, look at **File 7** — finding the `<AgentStudioProvider>` mount point and adding the `mcpClient` prop. Without it, the discovery button doesn't work. With it, the whole feature lights up.

Everything else is genuinely small and additive.
