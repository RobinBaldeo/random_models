# Co-pilot Prompts — Standalone MCP Tool Node (UI)

This file contains a prompt per change. Paste each one into Co-pilot (Opus 4.6) one at a time, against the relevant file open in your editor. Each prompt is scoped to a single file and includes the exact code blocks to insert.

**Apply order (important):**

1. Prompt 1 — `ir.ts` (type addition, no runtime impact)
2. Prompt 2 — `types/mcp.ts` (new file)
3. Prompt 3 — `clients/defaultMcpClient.ts` (new file)
4. Prompt 4 — `hooks/useMcpToolCatalog.ts` (new file)
5. Prompt 5 — `providers/AgentStudioProvider.tsx` (extend existing file)
6. Prompt 6 — `components/MCPServerInspector.tsx` (surgical inserts only — NOT a rewrite)
7. Prompt 7 — App root wiring (one line, find the mount point first)

After each prompt, run typecheck before moving to the next.

---

## Prompt 1 — Add three optional fields to `MCPServerNode` in `ir.ts`

**File:** `client/src/modules/agent-builder/model/ir.ts`

**Paste this prompt to Co-pilot:**

> In this file, locate the `MCPServerNode` interface. It currently has fields including `url`, `prefix`, `protocol`, `transport`, `auth`, `capabilities`, `namespaceFilter`, `metadata`, and `llmAttached` inside its `data` object.
>
> Append three new optional fields to the `data` object, keeping every existing field exactly as it is. Do not modify any other interface in this file. Do not reorder existing fields. Add the new fields at the bottom of the `data` block, after `llmAttached`:
>
> ```ts
> // Standalone-mode fields (used when llmAttached is false / undefined).
>
> /** Specific MCP tool this node should invoke when running standalone. */
> toolName?: string;
>
> /** Parameter overrides passed to the tool at runtime. */
> parameterOverrides?: Record<string, any>;
>
> /** Variable name to store the tool result under in graph state. */
> outputVarName?: string;
> ```
>
> All three fields are optional. The interface should remain backwards-compatible — existing code that constructs `MCPServerNode` without these fields must continue to typecheck.

---

## Prompt 2 — Create `types/mcp.ts` (new file)

**File:** `client/src/modules/agent-builder/types/mcp.ts` (does not exist yet — create it)

**Paste this prompt to Co-pilot:**

> Create a new file at `client/src/modules/agent-builder/types/mcp.ts` with the following exact contents. This file defines types for MCP tool discovery, mirroring the shape of `types/tools.ts` in the same directory.
>
> ```ts
> export type McpTransport = "HTTP" | "SSE";
>
> export type McpAuthType = "None" | "OBO" | "client_credentials" | "api_key" | "mtls";
>
> export type McpAuthConfig = {
>   type: McpAuthType;
>   scopes?: string[];
>   secretRef?: string | null;
> };
>
> /**
>  * One tool returned from an MCP server's catalog.
>  * Slimmer than ToolDefinition because MCP tools don't carry categories,
>  * versions, or per-tool auth.
>  */
> export type McpToolDefinition = {
>   name: string;
>   description?: string;
>   /** JSON-schema-like dict describing tool input parameters */
>   argsSchema?: Record<string, any>;
> };
>
> export type McpDiscoverQuery = {
>   url: string;
>   transport?: McpTransport;
>   auth?: McpAuthConfig;
>   /** Optional tenant context for tenant-scoped auth resolution */
>   tenantId?: string;
> };
>
> export type McpDiscoverResult = {
>   ok: boolean;
>   tools: McpToolDefinition[];
>   error?: string;
> };
>
> export interface McpClient {
>   /**
>    * Connects to the given MCP server and returns its tool catalog.
>    * Returns ok=false with an error message on connectivity/auth failures
>    * rather than throwing, so callers can render inline errors without
>    * try/catch ceremony.
>    */
>   discoverTools(query: McpDiscoverQuery): Promise<McpDiscoverResult>;
> }
> ```
>
> Do not add any imports; this file is pure type definitions.

---

## Prompt 3 — Create `clients/defaultMcpClient.ts` (new file)

**File:** `client/src/modules/agent-builder/clients/defaultMcpClient.ts` (does not exist yet — create it; create the `clients/` directory if it doesn't exist)

**Paste this prompt to Co-pilot:**

> Create a new file at `client/src/modules/agent-builder/clients/defaultMcpClient.ts`. If the `clients/` directory does not exist under `agent-builder/`, create it first.
>
> This file provides a fetch-based default implementation of the `McpClient` interface from `../types/mcp`. It calls the backend endpoint `POST /api/v2/mcp/discover` and normalizes the response.
>
> Use the following exact contents:
>
> ```ts
> import type {
>   McpClient,
>   McpDiscoverQuery,
>   McpDiscoverResult,
>   McpToolDefinition,
> } from "../types/mcp";
>
> /**
>  * Build a default fetch-based McpClient.
>  *
>  * baseUrl is optional; defaults to "" (relative URLs, same origin).
>  * Pass a non-empty baseUrl when the API is hosted on a different origin
>  * than the Studio UI.
>  */
> export function createDefaultMcpClient(baseUrl: string = ""): McpClient {
>   const endpoint: string = `${baseUrl}/api/v2/mcp/discover`;
>
>   return {
>     async discoverTools(query: McpDiscoverQuery): Promise<McpDiscoverResult> {
>       const body: Record<string, unknown> = {
>         url: query.url,
>         transport: query.transport ?? "HTTP",
>       };
>       if (query.auth) body.auth = query.auth;
>       if (query.tenantId) body.tenantId = query.tenantId;
>
>       let res: Response;
>       try {
>         res = await fetch(endpoint, {
>           method: "POST",
>           headers: { "Content-Type": "application/json" },
>           credentials: "include",
>           body: JSON.stringify(body),
>         });
>       } catch (err: any) {
>         return {
>           ok: false,
>           tools: [],
>           error: `Network error: ${err?.message ?? "fetch failed"}`,
>         };
>       }
>
>       if (!res.ok) {
>         let detail: string = `HTTP ${res.status}`;
>         try {
>           const text: string = await res.text();
>           if (text) detail = `${detail}: ${text.slice(0, 200)}`;
>         } catch {
>           // ignore body read errors
>         }
>         return { ok: false, tools: [], error: detail };
>       }
>
>       let data: any;
>       try {
>         data = await res.json();
>       } catch (err: any) {
>         return {
>           ok: false,
>           tools: [],
>           error: `Could not parse server response: ${err?.message ?? "parse error"}`,
>         };
>       }
>
>       const tools: McpToolDefinition[] = Array.isArray(data?.tools)
>         ? data.tools.map(
>             (t: any): McpToolDefinition => ({
>               name: String(t?.name ?? ""),
>               description: t?.description ?? undefined,
>               argsSchema: t?.argsSchema ?? t?.args_schema ?? undefined,
>             }),
>           )
>         : [];
>
>       return {
>         ok: Boolean(data?.ok),
>         tools,
>         error: data?.error ?? undefined,
>       };
>     },
>   };
> }
> ```
>
> Do not modify any existing files. The only import is from `../types/mcp` which was created in the previous step.

---

## Prompt 4 — Create `hooks/useMcpToolCatalog.ts` (new file)

**File:** `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts` (does not exist yet — create it)

**Paste this prompt to Co-pilot:**

> Create a new file at `client/src/modules/agent-builder/hooks/useMcpToolCatalog.ts`. This file should sit alongside the existing `useToolsCatalog.ts` in the same directory and follow the same patterns.
>
> The hook reads `mcpClient` from the AgentStudio context and exposes manual-refetch discovery state. Use the following exact contents:
>
> ```ts
> import { useCallback, useMemo, useState } from "react";
> import type { McpDiscoverQuery, McpToolDefinition } from "../types/mcp";
> import { useAgentStudio } from "../providers/AgentStudioProvider";
>
> export type UseMcpToolCatalogResult = {
>   tools: McpToolDefinition[];
>   isLoading: boolean;
>   error?: string;
>   /** Manually trigger a fetch with the given query. Idempotent. */
>   refetch: (query: McpDiscoverQuery) => Promise<void>;
>   /** Clear cached results — useful when the URL changes mid-edit. */
>   reset: () => void;
> };
>
> /**
>  * Hook for fetching the tool catalog from a configured MCP server.
>  * Manual refetch (rather than auto-fetching on every render) lets the user
>  * click "Discover Tools" explicitly, since hitting a remote MCP server is
>  * not free.
>  */
> export function useMcpToolCatalog(): UseMcpToolCatalogResult {
>   const { tenantId, mcpClient } = useAgentStudio();
>   const [tools, setTools] = useState<McpToolDefinition[]>([]);
>   const [error, setError] = useState<string | undefined>(undefined);
>   const [isLoading, setIsLoading] = useState<boolean>(false);
>
>   const refetch = useCallback(
>     async (query: McpDiscoverQuery): Promise<void> => {
>       if (!mcpClient) {
>         setError("MCP client not configured");
>         return;
>       }
>       if (!query.url || !query.url.trim()) {
>         setError("Server URL is required");
>         return;
>       }
>
>       setIsLoading(true);
>       setError(undefined);
>
>       try {
>         const effectiveQuery: McpDiscoverQuery = {
>           ...query,
>           tenantId: query.tenantId ?? tenantId,
>         };
>         const res = await mcpClient.discoverTools(effectiveQuery);
>         if (res.ok) {
>           setTools(res.tools);
>           setError(undefined);
>         } else {
>           setTools([]);
>           setError(res.error ?? "Discovery failed");
>         }
>       } catch (err: any) {
>         setTools([]);
>         setError(err?.message ?? String(err));
>       } finally {
>         setIsLoading(false);
>       }
>     },
>     [mcpClient, tenantId],
>   );
>
>   const reset = useCallback((): void => {
>     setTools([]);
>     setError(undefined);
>     setIsLoading(false);
>   }, []);
>
>   return useMemo(
>     (): UseMcpToolCatalogResult => ({
>       tools,
>       isLoading,
>       error,
>       refetch,
>       reset,
>     }),
>     [tools, isLoading, error, refetch, reset],
>   );
> }
> ```
>
> The imports rely on:
> - `../types/mcp` (created in Prompt 2)
> - `../providers/AgentStudioProvider` (existing file, will be extended in Prompt 5)
>
> Do not modify any other files in this step.

---

## Prompt 5 — Extend `AgentStudioProvider.tsx` to expose `mcpClient`

**File:** `client/src/modules/agent-builder/providers/AgentStudioProvider.tsx`

**Paste this prompt to Co-pilot:**

> In this file, make three small additive changes. Do not delete or rewrite any existing code. Do not touch the `useEffect` that bootstraps `toolsIndex` — leave it exactly as is.
>
> **Change 5.1 — Add a new import directly after the existing `import type { ToolsClient, ToolDefinition } from "../types/tools";` line:**
>
> ```ts
> import type { McpClient } from "../types/mcp";
> ```
>
> **Change 5.2 — Locate the `AgentStudioContextValue` type. It currently has fields `tenantId`, `toolsClient`, `featureFlags`, and `toolsIndex`. Add `mcpClient` as an optional field, immediately after `toolsClient`:**
>
> Existing code looks like:
> ```ts
> type AgentStudioContextValue = {
>   tenantId: string;
>   toolsClient: ToolsClient;
>   featureFlags?: FeatureFlags;
>   toolsIndex: Record<string, ToolDefinition>;
> };
> ```
>
> Update it to:
> ```ts
> type AgentStudioContextValue = {
>   tenantId: string;
>   toolsClient: ToolsClient;
>   mcpClient?: McpClient;
>   featureFlags?: FeatureFlags;
>   toolsIndex: Record<string, ToolDefinition>;
> };
> ```
>
> **Change 5.3 — Locate the `AgentStudioProvider` function. Update its destructured props and its prop type to accept an optional `mcpClient`. Then forward `mcpClient` through the context `value`. Also add `mcpClient` to the `useMemo` dependency array.**
>
> Existing destructure looks like:
> ```ts
> export function AgentStudioProvider({ tenantId, toolsClient, featureFlags, children }: { tenantId: string; toolsClient: ToolsClient; featureFlags?: FeatureFlags; children: React.ReactNode }): Element {
> ```
>
> Update it to:
> ```ts
> export function AgentStudioProvider({ tenantId, toolsClient, mcpClient, featureFlags, children }: { tenantId: string; toolsClient: ToolsClient; mcpClient?: McpClient; featureFlags?: FeatureFlags; children: React.ReactNode }): Element {
> ```
>
> Find the `useMemo` that builds `value` (looks like `useMemo<AgentStudioContextValue>(...)`). Add `mcpClient` to the returned object and to the dependency array. Existing:
> ```ts
> const value: AgentStudioContextValue = useMemo<AgentStudioContextValue>(() => ({ tenantId, toolsClient, featureFlags, toolsIndex }), [tenantId, toolsClient, featureFlags, toolsIndex]);
> ```
>
> Update to:
> ```ts
> const value: AgentStudioContextValue = useMemo<AgentStudioContextValue>(() => ({ tenantId, toolsClient, mcpClient, featureFlags, toolsIndex }), [tenantId, toolsClient, mcpClient, featureFlags, toolsIndex]);
> ```
>
> Do not change anything else in the file. `mcpClient` is optional, so existing tests and consumers continue to work without passing it.

---

## Prompt 6 — Surgical inserts to `MCPServerInspector.tsx`

**File:** `client/src/modules/agent-builder/components/MCPServerInspector.tsx`

**This is the most surgical prompt. Read it carefully before pasting.**

**Paste this prompt to Co-pilot:**

> In this file, make seven small additive changes. Do not rewrite the file. Do not modify the Connection, Capabilities, Status, or Validation tab content. Each existing tab's JSX block must remain functionally unchanged. Apply changes in order. Each change is independently safe.
>
> **Change 6.1 — Add three new imports at the top of the file, after the existing imports:**
>
> ```ts
> import { useMcpToolCatalog } from "../hooks/useMcpToolCatalog";
> ```
>
> If `useState`, `useEffect`, and `useMemo` aren't already imported from `"react"` at the top, add whichever are missing. They are likely already there.
>
> **Change 6.2 — Extend the `tab` state union to include `"Parameters"`.**
>
> Find the existing `useState` for `tab`, which looks like:
> ```ts
> const [tab, setTab] = useState<"Basics" | "Connection" | "Capabilities" | "Status" | "Validation">("Basics");
> ```
>
> Replace it with:
> ```ts
> const [tab, setTab] = useState<
>   "Basics" | "Connection" | "Capabilities" | "Parameters" | "Status" | "Validation"
> >("Basics");
> ```
>
> **Change 6.3 — Add `isAttachedToAgent` derived flag, the discovery hook, the `hasDiscovered` state, and a URL-change reset effect. Insert this block immediately after the existing `effectivePrefix` declaration (which is around the prefix-related useMemo block):**
>
> ```ts
> // Derived: is this MCP node currently attached to a codeless agent?
> // Set automatically by recomputeAttachedTools in the reducer based on graph
> // topology — read-only state.
> const isAttachedToAgent: boolean = Boolean(node?.data?.llmAttached);
>
> // MCP tool discovery hook — fetches the server's tool catalog on demand.
> const {
>   tools: discoveredTools,
>   isLoading: isDiscovering,
>   error: discoverError,
>   refetch: refetchTools,
>   reset: resetTools,
> } = useMcpToolCatalog();
>
> // Track whether discovery has been attempted at least once. Prevents the
> // "no tools found" empty state from showing before the user clicks.
> const [hasDiscovered, setHasDiscovered] = useState<boolean>(false);
>
> // If the URL changes after discovery, reset the cached tool list so the
> // dropdown doesn't show stale tools from a previous server.
> const currentUrl: string = String(node?.data?.url ?? "");
> useEffect((): void => {
>   if (hasDiscovered) {
>     resetTools();
>     setHasDiscovered(false);
>   }
>   // eslint-disable-next-line react-hooks/exhaustive-deps
> }, [currentUrl]);
> ```
>
> **Change 6.4 — Add a mode-dependent tab list and an effective-tab fallback. Insert this block immediately after the `update` helper definition (which dispatches `SET_GRAPH`):**
>
> ```ts
> // Tab list — "Parameters" only shown in standalone mode.
> const tabList: Array<
>   "Basics" | "Connection" | "Capabilities" | "Parameters" | "Status" | "Validation"
> > = isAttachedToAgent
>   ? ["Basics", "Connection", "Capabilities", "Status", "Validation"]
>   : ["Basics", "Connection", "Capabilities", "Parameters", "Status", "Validation"];
>
> const effectiveTab: typeof tab = tabList.includes(tab) ? tab : "Basics";
>
> // Discovery handler — fires when user clicks "Discover Tools".
> const handleDiscover = async (): Promise<void> => {
>   if (!currentUrl.trim()) return;
>   await refetchTools({
>     url: currentUrl,
>     transport: (node.data?.transport as "HTTP" | "SSE" | undefined) ?? "HTTP",
>     auth: node.data?.auth,
>   });
>   setHasDiscovered(true);
> };
>
> // Currently-selected tool definition (if discovered). Used to show description.
> const selectedToolDef = discoveredTools.find((t) => t.name === node.data?.toolName);
>
> const canDiscover: boolean = currentUrl.trim().length > 0 && !isDiscovering;
>
> // Variable expression preview for the standalone-mode "copy variable" helper.
> const outputVarExpr: string = `$.variables.${(node.data?.outputVarName || "").trim() || "<n>"}`;
>
> // --- Parameters tab JSON draft buffer ---
> const parameterOverridesObj: Record<string, any> =
>   (node.data?.parameterOverrides as Record<string, any> | undefined) ?? {};
> const [paramJsonDraft, setParamJsonDraft] = useState<string>((): string => {
>   try {
>     return JSON.stringify(parameterOverridesObj, null, 2);
>   } catch {
>     return "{}";
>   }
> });
> const [paramJsonError, setParamJsonError] = useState<string | null>(null);
>
> useEffect((): void => {
>   try {
>     const serialized: string = JSON.stringify(parameterOverridesObj, null, 2);
>     if (serialized !== paramJsonDraft && paramJsonError === null) {
>       setParamJsonDraft(serialized);
>     }
>   } catch {
>     // ignore
>   }
>   // eslint-disable-next-line react-hooks/exhaustive-deps
> }, [JSON.stringify(parameterOverridesObj)]);
>
> const onParamJsonChange: (val: string) => void = (val: string): void => {
>   setParamJsonDraft(val);
>   if (val.trim() === "") {
>     setParamJsonError(null);
>     update({ parameterOverrides: {} });
>     return;
>   }
>   try {
>     const parsed: unknown = JSON.parse(val);
>     if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
>       setParamJsonError(null);
>       update({ parameterOverrides: parsed as Record<string, any> });
>     } else {
>       setParamJsonError("Parameter overrides must be a JSON object.");
>     }
>   } catch (err: any) {
>     setParamJsonError(`Invalid JSON: ${err?.message ?? "parse error"}`);
>   }
> };
> ```
>
> **Change 6.5 — Update the tab navigation to use `tabList` and `effectiveTab`.**
>
> Find the `<nav>` element with `role="tablist"`. It currently maps over a hardcoded array like `(["Basics", "Connection", "Capabilities", "Status", "Validation"] as const)`. Replace that with mapping over `tabList`. Inside the button, replace any reference to `tab === t` with `effectiveTab === t`.
>
> Then find every conditional render block in the file that looks like `{tab === "Basics" && (...)}`, `{tab === "Connection" && (...)}`, etc. Replace `tab` with `effectiveTab` in all of them. Do not change the JSX inside those blocks. Just change the gate from `tab` to `effectiveTab`.
>
> **Change 6.6 — Inside the existing Basics tab block (the `{effectiveTab === "Basics" && (...)}` block, just renamed from `{tab === "Basics" && ...}`), add the standalone-mode tool discovery UI and output variable name fields at the very bottom — after the existing Description label but still inside the Basics block.**
>
> Insert this JSX inside the Basics block, after the closing `</label>` of the Description field:
>
> ```tsx
> {!isAttachedToAgent && (
>   <>
>     <div
>       className="ab-field ab-field--stack"
>       style={{
>         paddingTop: "0.75rem",
>         marginTop: "0.75rem",
>         borderTop: "1px solid #e5e7eb",
>       }}
>     >
>       <span>Tool</span>
>
>       <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
>         <button
>           type="button"
>           className="ab-btn ab-btn--primary"
>           onClick={handleDiscover}
>           disabled={!canDiscover}
>           title={
>             !currentUrl.trim()
>               ? "Enter a server URL on the Connection tab first"
>               : "Fetch tools from the configured MCP server"
>           }
>         >
>           {isDiscovering ? "Discovering…" : "Discover Tools"}
>         </button>
>         {hasDiscovered && !isDiscovering && discoveredTools.length > 0 && (
>           <span className="ab-help" style={{ fontSize: "0.875rem" }}>
>             Found {discoveredTools.length} tool{discoveredTools.length === 1 ? "" : "s"}
>           </span>
>         )}
>       </div>
>
>       {discoverError && (
>         <div
>           className="ab-help"
>           role="alert"
>           aria-live="polite"
>           style={{ color: "#b91c1c", marginTop: 6 }}
>         >
>           {discoverError}
>         </div>
>       )}
>
>       {hasDiscovered &&
>         !isDiscovering &&
>         discoveredTools.length === 0 &&
>         !discoverError && (
>           <div className="ab-help" style={{ marginTop: 6 }}>
>             No tools found on this server.
>           </div>
>         )}
>
>       {(discoveredTools.length > 0 ||
>         (node.data?.toolName && !hasDiscovered)) && (
>         <select
>           value={node.data?.toolName ?? ""}
>           onChange={(e) => update({ toolName: e.target.value })}
>           style={{ marginTop: 6 }}
>         >
>           <option value="">— Select a tool —</option>
>           {node.data?.toolName &&
>             !discoveredTools.some((t) => t.name === node.data.toolName) && (
>               <option value={node.data.toolName}>
>                 {node.data.toolName} (not in current catalog)
>               </option>
>             )}
>           {discoveredTools.map((t) => (
>             <option key={t.name} value={t.name}>
>               {t.name}
>             </option>
>           ))}
>         </select>
>       )}
>
>       {selectedToolDef?.description && (
>         <div className="ab-help" style={{ marginTop: 6 }}>
>           {selectedToolDef.description}
>         </div>
>       )}
>
>       <div className="ab-help" style={{ marginTop: 6 }}>
>         Click <strong>Discover Tools</strong> to fetch the server's tool catalog,
>         then pick the one this node should invoke.
>       </div>
>     </div>
>
>     <label className="ab-field ab-field--stack">
>       <span>Output variable name</span>
>       <input
>         value={node.data?.outputVarName || ""}
>         onChange={(e) => update({ outputVarName: e.target.value })}
>         placeholder="e.g. mcpResult"
>       />
>       <div className="ab-help">
>         Results from the tool are stored under this variable for downstream nodes.
>       </div>
>       <div style={{ marginTop: 8, display: "flex", gap: "0.5rem" }}>
>         <button
>           type="button"
>           className="ab-btn ab-btn--outline"
>           onClick={() => {
>             if (navigator?.clipboard?.writeText) {
>               navigator.clipboard.writeText(outputVarExpr);
>             }
>           }}
>           disabled={!node.data?.outputVarName}
>           title="Copy $.variables.[name] to clipboard"
>         >
>           Copy variable ({outputVarExpr})
>         </button>
>       </div>
>     </label>
>   </>
> )}
> ```
>
> **Change 6.7 — Add a new Parameters tab block. Insert it immediately AFTER the Capabilities tab block (`{effectiveTab === "Capabilities" && (...)}`) and BEFORE the Status tab block:**
>
> ```tsx
> {effectiveTab === "Parameters" && !isAttachedToAgent && (
>   <div
>     className="ab-inspector__section"
>     id="mcp-tab-panel-parameters"
>     role="tabpanel"
>     aria-labelledby="mcp-tab-parameters"
>   >
>     <h3>Parameters</h3>
>     <p className="ab-inspector__section-hint">
>       Provide parameter overrides for the selected MCP tool, as a JSON object.
>       Variable references like <code>{"$.variables.someName"}</code> are resolved
>       at runtime.
>     </p>
>
>     {selectedToolDef?.argsSchema && (
>       <div
>         className="ab-help"
>         style={{
>           marginBottom: "0.5rem",
>           padding: "0.5rem",
>           background: "#f9fafb",
>           border: "1px solid #e5e7eb",
>           borderRadius: 4,
>         }}
>       >
>         <strong>Tool args schema:</strong>{" "}
>         <code style={{ fontSize: "0.75rem" }}>
>           {JSON.stringify(selectedToolDef.argsSchema)}
>         </code>
>       </div>
>     )}
>
>     <label className="ab-field ab-field--stack">
>       <span>Parameter overrides (JSON)</span>
>       <AutoTextarea
>         value={paramJsonDraft}
>         onChange={onParamJsonChange}
>         minRows={6}
>         placeholder={`{\n  "message": "hello",\n  "ttl_seconds": 3600\n}`}
>       />
>       {paramJsonError ? (
>         <div
>           className="ab-help"
>           style={{ color: "#b91c1c" }}
>           role="alert"
>           aria-live="polite"
>         >
>           {paramJsonError}
>         </div>
>       ) : (
>         <div className="ab-help">
>           Keys must match the tool's expected input fields.
>         </div>
>       )}
>     </label>
>     <div style={{ marginTop: "0.5rem" }}>
>       <button
>         type="button"
>         className="ab-btn ab-btn--outline"
>         onClick={() => {
>           setParamJsonDraft("{}");
>           setParamJsonError(null);
>           update({ parameterOverrides: {} });
>         }}
>       >
>         Reset overrides
>       </button>
>     </div>
>   </div>
> )}
> ```
>
> Verify after applying:
> - The Connection, Capabilities, Status, and Validation tab JSX blocks are still present and unchanged.
> - All `{tab === "..."}` conditional renders have become `{effectiveTab === "..."}`.
> - The new Basics-tab additions are inside the existing Basics block and only render when `!isAttachedToAgent`.
> - The new Parameters tab block sits between Capabilities and Status.
>
> Do not introduce a mode banner. Do not delete or rewrite any existing tab content. The total line addition should be approximately 200 lines, all confined to the changes listed above.

---

## Prompt 7 — Wire `mcpClient` into the app root (find the mount point first)

**File:** Wherever `<AgentStudioProvider>` is mounted in your app (likely `App.tsx`, `index.tsx`, or a layout/route file)

**To find the mount point first**, run in your project root:

```bash
grep -rn "<AgentStudioProvider" client/src --include="*.tsx" --include="*.ts"
```

The output will show the file containing the `<AgentStudioProvider ...>` JSX element. Open that file and apply the prompt below.

**Paste this prompt to Co-pilot:**

> In this file, find the `<AgentStudioProvider>` JSX element. It currently passes `tenantId`, `toolsClient`, and possibly `featureFlags` as props. Add a new `mcpClient` prop that uses the default fetch-based MCP client.
>
> **Step 1 — Add the import** near the top of the file with the other imports:
>
> ```ts
> import { createDefaultMcpClient } from "<path>/modules/agent-builder/clients/defaultMcpClient";
> ```
>
> Adjust the path to match how this file imports other agent-builder modules (e.g. relative path or absolute alias).
>
> **Step 2 — Construct the client** at module scope or just before the JSX (whichever matches existing style):
>
> ```ts
> const mcpClient = createDefaultMcpClient();
> ```
>
> If this codebase has an `API_BASE_URL` constant, pass it: `createDefaultMcpClient(API_BASE_URL)`.
>
> **Step 3 — Add the prop** to the existing `<AgentStudioProvider>` JSX:
>
> Existing JSX likely looks like:
> ```tsx
> <AgentStudioProvider tenantId={tenantId} toolsClient={toolsClient} featureFlags={featureFlags}>
>   {children}
> </AgentStudioProvider>
> ```
>
> Update it to add the `mcpClient` prop:
> ```tsx
> <AgentStudioProvider
>   tenantId={tenantId}
>   toolsClient={toolsClient}
>   mcpClient={mcpClient}
>   featureFlags={featureFlags}
> >
>   {children}
> </AgentStudioProvider>
> ```
>
> Do not change any other props or restructure the surrounding JSX. The existing component shape and child rendering must remain identical.

---

## Verification after applying all 7 prompts

Run in order:

```bash
# 1. Typecheck — should pass
npm run typecheck

# 2. Existing tests — should pass
npm test -- MCPServerInspector
npm test -- AgentStudioProvider

# 3. Start dev server, smoke-test the UI manually:
#    - Drop an mcpServer node on the canvas (no agent connected)
#    - Connection tab → enter http://localhost:8123 (your stub server URL)
#    - Basics tab → click "Discover Tools" → dropdown populates
#    - Pick a tool → description shows
#    - Parameters tab → enter {"message": "hello"} → set outputVarName to "result"
#    - Save the graph → run it → tool fires → $.variables.result populated
#    - Drag edge from a codeless agent to the mcpServer node → standalone UI hides
#    - Disconnect → standalone UI returns
#    - Bad URL → "Discover Tools" → inline red error
```

If any of these fail, the failure is scoped to one prompt's worth of changes and is easy to localize.
