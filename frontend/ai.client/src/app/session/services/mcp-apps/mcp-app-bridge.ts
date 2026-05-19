/**
 * MCP Apps host bridge (SEP-1865), PR #4 of
 * `docs/kaizen/scoping/mcp-apps-host-renderer.md`.
 *
 * The host half of the JSON-RPC-2.0-over-postMessage protocol. It talks to
 * the deployed sandbox-proxy (`proxy.html`, a different origin); the proxy
 * forwards to/from the App View running in its inner null-origin iframe.
 *
 * Security model (spec + scoping doc decision #1/#5):
 *  - Outer proxy iframe lives at a dedicated origin; messages to/from it are
 *    validated by `event.source === proxyWindow` AND
 *    `event.origin === sandboxOrigin`.
 *  - A per-frame nonce, minted here and handed to the proxy in
 *    `sandbox-resource-ready`, authenticates every subsequent exchange (the
 *    inner View is null-origin, so the nonce — not origin — is the real
 *    check the spec mandates).
 *  - The host MUST NOT send any request/notification toward the View before
 *    it observes the View's `ui/notifications/initialized`; pre-init sends
 *    are queued and flushed on initialized.
 *
 * Framework-free and DOM-light on purpose: `hostWindow` and the proxy window
 * accessor are injected so specs drive it with plain fakes (no vi.mock of
 * globals — house rule).
 *
 * Out of scope for PR #4 (owned by PR #5/#6, answered with JSON-RPC errors
 * so Apps degrade gracefully): app-initiated `tools/call` proxying,
 * `ui/message`, `ui/update-model-context`, and `ui/open-link` consent
 * gating (PR #4 opens links directly via the injected opener).
 */

import type {
  UiResourceEvent,
} from '../../../shared/utils/stream-parser';
import {
  HostContext,
  JsonRpcId,
  JsonRpcMessage,
  JsonRpcRequest,
  JSONRPC_IMPL_ERROR,
  JSONRPC_METHOD_NOT_FOUND,
  M_HOST_CONTEXT_CHANGED,
  M_MESSAGE,
  M_OPEN_LINK,
  M_PING,
  M_REQUEST_DISPLAY_MODE,
  M_RESOURCE_TEARDOWN,
  M_SANDBOX_PROXY_READY,
  M_SANDBOX_RESOURCE_READY,
  M_SIZE_CHANGED,
  M_TOOL_CANCELLED,
  M_TOOL_INPUT,
  M_TOOL_RESULT,
  M_UI_INITIALIZE,
  M_UI_INITIALIZED,
  M_UPDATE_MODEL_CONTEXT,
  MCP_UI_PROTOCOL_VERSION,
  SizeChangedParams,
  isJsonRpc,
  isRequest,
} from './mcp-app-protocol';

/** Minimal window surface the bridge needs (eases testing). */
export interface BridgeHostWindow {
  addEventListener(
    type: 'message',
    listener: (ev: MessageEvent) => void,
  ): void;
  removeEventListener(
    type: 'message',
    listener: (ev: MessageEvent) => void,
  ): void;
}

export interface McpAppBridgeDeps {
  /** The window that hosts the outer proxy iframe (for `message` events). */
  hostWindow: BridgeHostWindow;
  /** Lazily resolves the proxy iframe's `contentWindow` (null until load). */
  getProxyWindow: () => Window | null;
  /** Origin the proxy is served from; targetOrigin + inbound origin check. */
  sandboxOrigin: string;
  /** The resource (html/csp/permissions) to render. */
  resource: UiResourceEvent;
  /** Per-frame nonce (mint once per frame; never reused). */
  nonce: string;
  /** Complete tool-call arguments for `ui/notifications/tool-input`. */
  getToolInput: () => Record<string, unknown>;
  /** Tool result as an MCP `CallToolResult`, or null if not yet available. */
  getToolResult: () => unknown | null;
  /** Current host UI context (theme, displayMode, …). */
  getHostContext: () => HostContext;
  /** Open an external URL (PR #4: direct; PR #6 adds consent). */
  openLink: (url: string) => void;
  /** Non-fatal diagnostics (validation drops, protocol slips). */
  onWarn?: (message: string) => void;
}

export class McpAppBridge {
  private readonly d: McpAppBridgeDeps;
  private listener: ((ev: MessageEvent) => void) | null = null;

  /** Set once `sandbox-resource-ready` is sent — nonce now required. */
  private nonceArmed = false;
  /** Set on the View's `ui/notifications/initialized`. */
  private viewInitialized = false;
  private disposed = false;

  /** Notifications deferred until the View reports `initialized`. */
  private readonly preInitQueue: Array<{ method: string; params: unknown }> = [];

  /** Whether tool-input was already pushed (spec: at most once). */
  private toolInputSent = false;

  /** Pending host→View requests awaiting a JSON-RPC response, by id. */
  private readonly pending = new Map<
    JsonRpcId,
    { resolve: (v: unknown) => void; reject: (e: unknown) => void }
  >();
  private nextRequestId = 1;

  constructor(deps: McpAppBridgeDeps) {
    this.d = deps;
  }

  /** Begin listening. Call once the outer iframe element exists. */
  start(): void {
    if (this.listener) return;
    this.listener = (ev: MessageEvent) => this.onMessage(ev);
    this.d.hostWindow.addEventListener('message', this.listener);
  }

  /**
   * Tear down: best-effort `ui/resource-teardown` toward the View, then
   * detach. Safe to call multiple times.
   */
  dispose(reason = 'host-teardown'): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.viewInitialized) {
      // Fire-and-forget: we're going away regardless of the ack.
      this.sendRequest(M_RESOURCE_TEARDOWN, { reason }).catch(() => undefined);
    }
    if (this.listener) {
      this.d.hostWindow.removeEventListener('message', this.listener);
      this.listener = null;
    }
    for (const { reject } of this.pending.values()) {
      reject(new Error('bridge disposed'));
    }
    this.pending.clear();
  }

  /** Push a `host-context-changed` partial (e.g., theme toggle). */
  notifyHostContextChanged(partial: Partial<HostContext>): void {
    this.sendNotification(M_HOST_CONTEXT_CHANGED, partial);
  }

  // --- inbound ------------------------------------------------------------

  private onMessage(ev: MessageEvent): void {
    if (this.disposed) return;
    const proxyWindow = this.d.getProxyWindow();
    // Source + origin gate. The proxy page is served from sandboxOrigin, so
    // its window's origin is a real URL (the null-origin inner frame only
    // ever talks to the proxy, never to us).
    if (!proxyWindow || ev.source !== proxyWindow) return;
    if (ev.origin !== this.d.sandboxOrigin) return;

    const data = ev.data;
    if (!isJsonRpc(data)) return;

    // Nonce gate: armed the moment we hand the proxy the nonce. The only
    // legitimately pre-nonce message is the proxy's first ready ping.
    const method = 'method' in data ? (data as { method?: string }).method : undefined;
    if (this.nonceArmed) {
      if ((data as { nonce?: string }).nonce !== this.d.nonce) {
        this.d.onWarn?.(`dropped message with bad/absent nonce (${method ?? 'response'})`);
        return;
      }
    } else if (method !== M_SANDBOX_PROXY_READY) {
      this.d.onWarn?.(`dropped pre-handshake message (${method ?? 'response'})`);
      return;
    }

    this.route(data);
  }

  private route(msg: JsonRpcMessage): void {
    // Response to a host-initiated request (e.g. resource-teardown).
    if (!('method' in msg) && 'id' in msg) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if ('error' in msg) p.reject(msg.error);
      else p.resolve((msg as { result: unknown }).result);
      return;
    }

    const method = (msg as { method: string }).method;
    switch (method) {
      case M_SANDBOX_PROXY_READY:
        this.sendSandboxResourceReady();
        return;

      case M_UI_INITIALIZE:
        this.handleInitialize(msg as JsonRpcRequest);
        return;

      case M_UI_INITIALIZED:
        this.viewInitialized = true;
        this.flushPreInit();
        this.pushToolData();
        return;

      case M_SIZE_CHANGED: {
        const p = (msg as { params?: SizeChangedParams }).params;
        if (p && typeof p.height === 'number') {
          this.sizeChangedCb?.(p.width, p.height);
        }
        return;
      }

      case M_PING:
        if (isRequest(msg)) this.respond(msg.id, {});
        return;

      case M_OPEN_LINK: {
        if (!isRequest(msg)) return;
        const url = (msg.params as { url?: string } | undefined)?.url;
        if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) {
          this.respondError(msg.id, JSONRPC_IMPL_ERROR, 'Invalid URL');
          return;
        }
        // PR #4 opens directly; per-link consent is PR #6.
        this.d.openLink(url);
        this.respond(msg.id, {});
        return;
      }

      case M_REQUEST_DISPLAY_MODE: {
        if (!isRequest(msg)) return;
        // PR #4 host only renders inline; spec: MUST return the resulting
        // mode (the current one when the request can't be honored).
        this.respond(msg.id, { mode: 'inline' });
        return;
      }

      case M_MESSAGE:
      case M_UPDATE_MODEL_CONTEXT:
        // Owned by PR #6. Answer per JSON-RPC so Apps degrade gracefully.
        if (isRequest(msg)) {
          this.respondError(
            msg.id,
            JSONRPC_METHOD_NOT_FOUND,
            `${method} not supported yet (PR #6)`,
          );
        }
        return;

      default:
        // Unknown request → method-not-found; unknown notification → ignore.
        if (isRequest(msg)) {
          this.respondError(
            msg.id,
            JSONRPC_METHOD_NOT_FOUND,
            `Unknown method: ${method}`,
          );
        }
        return;
    }
  }

  private handleInitialize(req: JsonRpcRequest): void {
    // A response (not a request/notification toward the View) — allowed
    // before `initialized`.
    this.respond(req.id, {
      protocolVersion: MCP_UI_PROTOCOL_VERSION,
      hostInfo: { name: 'agentcore-public-stack', version: '1.0.0' },
      hostCapabilities: {
        openLinks: {},
        sandbox: {
          permissions: this.d.resource.permissions,
          csp: this.d.resource.csp,
        },
      },
      hostContext: {
        ...this.d.getHostContext(),
        displayMode: 'inline',
        availableDisplayModes: ['inline'],
      },
    });
  }

  // --- outbound -----------------------------------------------------------

  private sizeChangedCb: ((w: number, h: number) => void) | null = null;
  /** Register the size-changed sink (the component resizes the iframe). */
  onSizeChanged(cb: (w: number, h: number) => void): void {
    this.sizeChangedCb = cb;
  }

  private sendSandboxResourceReady(): void {
    // Reserved host→proxy notification; consumed by the proxy, not forwarded.
    this.postToProxy({
      jsonrpc: '2.0',
      method: M_SANDBOX_RESOURCE_READY,
      params: {
        html: this.d.resource.html,
        sandbox: 'allow-scripts',
        csp: this.d.resource.csp,
        permissions: this.d.resource.permissions,
        nonce: this.d.nonce,
      },
    });
    // From here on every inbound message MUST carry the nonce.
    this.nonceArmed = true;
  }

  /** Push `tool-input` (once) then `tool-result` if it's available. */
  private pushToolData(): void {
    if (!this.toolInputSent) {
      this.toolInputSent = true;
      this.sendNotification(M_TOOL_INPUT, { arguments: this.d.getToolInput() });
    }
    const result = this.d.getToolResult();
    if (result != null) {
      this.sendNotification(M_TOOL_RESULT, result);
    }
  }

  /** Re-push the tool result if it arrives/changes after init. */
  refreshToolResult(): void {
    if (!this.viewInitialized) return;
    const result = this.d.getToolResult();
    if (result != null) this.sendNotification(M_TOOL_RESULT, result);
  }

  notifyToolCancelled(reason: string): void {
    this.sendNotification(M_TOOL_CANCELLED, { reason });
  }

  private sendNotification(method: string, params: unknown): void {
    // Spec: the host MUST NOT send any request/notification toward the View
    // before `initialized`. (The `sandbox-resource-ready` notification and
    // the `ui/initialize` response are bootstrap — they go straight through
    // `postToProxy`/`respond`, never here.)
    if (!this.viewInitialized) {
      this.preInitQueue.push({ method, params });
      return;
    }
    this.postToProxy({ jsonrpc: '2.0', method, params, nonce: this.d.nonce });
  }

  private sendRequest(method: string, params: unknown): Promise<unknown> {
    const id = `host-${this.nextRequestId++}`;
    const promise = new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.postToProxy({ jsonrpc: '2.0', id, method, params, nonce: this.d.nonce });
    return promise;
  }

  private respond(id: JsonRpcId, result: unknown): void {
    this.postToProxy({ jsonrpc: '2.0', id, result, nonce: this.d.nonce });
  }

  private respondError(id: JsonRpcId, code: number, message: string): void {
    this.postToProxy({
      jsonrpc: '2.0',
      id,
      error: { code, message },
      nonce: this.d.nonce,
    });
  }

  private flushPreInit(): void {
    const queued = this.preInitQueue.splice(0, this.preInitQueue.length);
    for (const { method, params } of queued) {
      this.postToProxy({ jsonrpc: '2.0', method, params, nonce: this.d.nonce });
    }
  }

  private postToProxy(msg: JsonRpcMessage): void {
    const w = this.d.getProxyWindow();
    if (!w) {
      this.d.onWarn?.('proxy window unavailable; message dropped');
      return;
    }
    // Strict targetOrigin: we know exactly where the proxy lives.
    w.postMessage(msg, this.d.sandboxOrigin);
  }
}
