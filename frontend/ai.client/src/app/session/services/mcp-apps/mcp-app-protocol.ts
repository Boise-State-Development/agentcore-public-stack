/**
 * MCP Apps (SEP-1865) host-half protocol types.
 *
 * JSON-RPC 2.0 envelopes exchanged over `postMessage` between this host and
 * the sandbox-proxy (and, through it, the App View). Normative reference:
 * `specification/2026-01-26/apps.mdx` in `modelcontextprotocol/ext-apps`.
 *
 * PR #4 of `docs/kaizen/scoping/mcp-apps-host-renderer.md`. Pure types +
 * constants; no Angular, no DOM — kept framework-free so the bridge is
 * unit-testable with plain fakes.
 */

import type { McpUiCsp, McpUiPermissions } from '../../../shared/utils/stream-parser';

/** Protocol version this host implements (the normative dated spec). */
export const MCP_UI_PROTOCOL_VERSION = '2026-01-26';

/** JSON-RPC id (spec allows string or number). */
export type JsonRpcId = string | number;

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: JsonRpcId;
  method: string;
  params?: unknown;
  /** Per-frame transport nonce (host↔proxy auth; not part of JSON-RPC). */
  nonce?: string;
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
  nonce?: string;
}

export interface JsonRpcSuccess {
  jsonrpc: '2.0';
  id: JsonRpcId;
  result: unknown;
  nonce?: string;
}

export interface JsonRpcError {
  jsonrpc: '2.0';
  id: JsonRpcId;
  error: { code: number; message: string; data?: unknown };
  nonce?: string;
}

export type JsonRpcMessage =
  | JsonRpcRequest
  | JsonRpcNotification
  | JsonRpcSuccess
  | JsonRpcError;

/** JSON-RPC reserved code for "method not found". */
export const JSONRPC_METHOD_NOT_FOUND = -32601;
/** Implementation-defined error (spec uses -32000 for ui/* denials). */
export const JSONRPC_IMPL_ERROR = -32000;

// --- Reserved sandbox-proxy methods (host ↔ proxy only) --------------------

/** Sandbox proxy → host: proxy bootstrapped, ready for the resource. */
export const M_SANDBOX_PROXY_READY = 'ui/notifications/sandbox-proxy-ready';
/** Host → sandbox proxy: raw HTML + sandbox/CSP/permissions to load. */
export const M_SANDBOX_RESOURCE_READY = 'ui/notifications/sandbox-resource-ready';

// --- Lifecycle / app methods (host ↔ View, forwarded by the proxy) --------

export const M_UI_INITIALIZE = 'ui/initialize';
export const M_UI_INITIALIZED = 'ui/notifications/initialized';
export const M_PING = 'ping';
/** Standard MCP method an App calls to invoke a server tool (PR #5). */
export const M_TOOLS_CALL = 'tools/call';

export const M_TOOL_INPUT = 'ui/notifications/tool-input';
export const M_TOOL_INPUT_PARTIAL = 'ui/notifications/tool-input-partial';
export const M_TOOL_RESULT = 'ui/notifications/tool-result';
export const M_TOOL_CANCELLED = 'ui/notifications/tool-cancelled';
export const M_RESOURCE_TEARDOWN = 'ui/resource-teardown';
export const M_HOST_CONTEXT_CHANGED = 'ui/notifications/host-context-changed';
export const M_SIZE_CHANGED = 'ui/notifications/size-changed';

export const M_OPEN_LINK = 'ui/open-link';
export const M_REQUEST_DISPLAY_MODE = 'ui/request-display-mode';
export const M_MESSAGE = 'ui/message';
export const M_UPDATE_MODEL_CONTEXT = 'ui/update-model-context';

export type DisplayMode = 'inline' | 'fullscreen' | 'pip';

/** Capabilities this host advertises to the View in `ui/initialize`. */
export interface HostCapabilities {
  /** Host can open external links (consent gating lands in PR #6). */
  openLinks?: Record<string, never>;
  /** Host can proxy the App's `tools/call` to the MCP server (PR #5). */
  serverTools?: Record<string, never>;
  /** Sandbox config the host applied (mirrors what we asked the proxy for). */
  sandbox?: {
    permissions?: McpUiPermissions;
    csp?: McpUiCsp;
  };
}

export interface HostContext {
  theme?: 'light' | 'dark';
  displayMode?: DisplayMode;
  availableDisplayModes?: DisplayMode[];
  locale?: string;
  timeZone?: string;
  userAgent?: string;
}

export interface McpUiInitializeResult {
  protocolVersion: string;
  hostCapabilities: HostCapabilities;
  hostInfo: { name: string; version: string };
  hostContext: HostContext;
}

/** Params for `ui/notifications/sandbox-resource-ready` (host → proxy). */
export interface SandboxResourceReadyParams {
  html: string;
  /**
   * Inner-iframe `sandbox` attribute. Host default matches the ext-apps
   * basic-host reference: `allow-scripts allow-same-origin allow-forms`.
   */
  sandbox?: string;
  csp?: McpUiCsp;
  permissions?: McpUiPermissions;
  /** Per-frame nonce the proxy must echo on every later message. */
  nonce: string;
}

export interface SizeChangedParams {
  width: number;
  height: number;
}

export interface OpenLinkParams {
  url: string;
}

export interface RequestDisplayModeParams {
  mode: DisplayMode;
}

/**
 * Spec shape of `ui/message` params (View → host). Per SEP-1865 / the
 * ext-apps SDK, `content` is an ARRAY of content blocks (the View sends
 * `content: [{ type: 'text', text }]`), not a single block.
 */
export interface MessageParams {
  role: 'user';
  content: Array<{ type: string; text?: string }>;
}

/** Spec shape of `ui/update-model-context` params (View → host). */
export interface UpdateModelContextParams {
  content?: unknown[];
  structuredContent?: Record<string, unknown>;
}

/** Sandbox capability keys an App may request (`_meta.ui.permissions`). */
export type CapabilityKey =
  | 'camera'
  | 'microphone'
  | 'geolocation'
  | 'clipboardWrite';

/**
 * A user-consent decision the host must obtain before acting on an
 * App-initiated request. PR #6 of
 * `docs/kaizen/scoping/mcp-apps-host-renderer.md`, decision: consent is
 * **frontend-only** — these requests originate from a postMessage on a
 * possibly-idle iframe, so there is no backend agent turn to pause (unlike
 * the OAuth-tool `oauth_required` SSE family). The host resolves them with
 * an inline in-thread prompt and gates the bridge response on the answer.
 */
export type ConsentRequest =
  | { kind: 'open-link'; url: string }
  | { kind: 'capabilities'; capabilities: CapabilityKey[] };

/** Narrowing helpers. */
export function isJsonRpc(data: unknown): data is JsonRpcMessage {
  return (
    !!data &&
    typeof data === 'object' &&
    (data as { jsonrpc?: unknown }).jsonrpc === '2.0'
  );
}

export function isRequest(m: JsonRpcMessage): m is JsonRpcRequest {
  return 'method' in m && 'id' in m;
}

export function isNotification(m: JsonRpcMessage): m is JsonRpcNotification {
  return 'method' in m && !('id' in m);
}
