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
 * PR #6 implements the remaining View→host methods: `ui/message` (relayed
 * to the chat send path — a real streaming turn, identical to a typed
 * message), `ui/update-model-context` (relayed to app-api → stashed on the
 * agent's Strands state for the next turn), and `ui/open-link` consent
 * gating (frontend-only — the request originates here, so there is no
 * backend turn to pause; the host obtains an inline in-thread consent
 * before opening). Each new dep is optional: when absent the bridge keeps
 * PR #4/#5 behavior (method-not-found / direct open) so older hosts and
 * specs degrade gracefully.
 */

import type {
  UiResourceEvent,
} from '../../../shared/utils/stream-parser';
import {
  ConsentRequest,
  DisplayMode,
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
  RequestDisplayModeParams,
  M_RESOURCE_TEARDOWN,
  M_SANDBOX_PROXY_READY,
  M_SANDBOX_RESOURCE_READY,
  M_SIZE_CHANGED,
  M_TOOLS_CALL,
  M_TOOL_CANCELLED,
  M_TOOL_INPUT,
  M_TOOL_INPUT_PARTIAL,
  M_TOOL_RESULT,
  M_UI_INITIALIZE,
  M_UI_INITIALIZED,
  M_UPDATE_MODEL_CONTEXT,
  MCP_UI_PROTOCOL_VERSION,
  MessageParams,
  SizeChangedParams,
  UpdateModelContextParams,
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
  /**
   * Latest server-healed streamed PARTIAL tool input, or null if none yet
   * (SEP-1865 `tool-input-partial`). Optional: absent ⇒ no progressive
   * streaming, the bridge sends only the complete `tool-input` (PR #4
   * behavior). The component drives subsequent partials via
   * `sendToolInputPartial`; this getter only seeds the value at init time.
   */
  getPartialToolInput?: () => Record<string, unknown> | null;
  /**
   * Whether the tool's input has finished streaming. Optional: absent ⇒
   * treated as final (PR #4 behavior — send the complete `tool-input` at
   * init). When present and false at init, the bridge sends the latest
   * partial instead and waits for the component to call `sendToolInputFinal`.
   */
  isToolInputFinal?: () => boolean;
  /** Tool result as an MCP `CallToolResult`, or null if not yet available. */
  getToolResult: () => unknown | null;
  /** Current host UI context (theme, displayMode, …). */
  getHostContext: () => HostContext;
  /** Open an external URL once consent (if wired) is granted. */
  openLink: (url: string) => void;
  /**
   * Proxy an App-initiated `tools/call` to the MCP server (PR #5) via
   * app-api. Resolves with the `CallToolResult`; rejects with an Error
   * whose message is safe to return to the App. Optional so older hosts
   * (and tests) can omit it — absent ⇒ `tools/call` is method-not-found.
   */
  proxyToolCall?: (
    toolName: string,
    args: Record<string, unknown>,
  ) => Promise<{ content: unknown[]; isError: boolean }>;
  /**
   * Relay `ui/message` as a real user turn (PR #6). The host treats it
   * identically to a typed message — it starts a normal streaming turn.
   * Absent (older hosts / specs) ⇒ `ui/message` is method-not-found.
   */
  sendMessage?: (text: string) => Promise<void>;
  /**
   * Relay `ui/update-model-context` (PR #6) to app-api, which stashes it
   * on the conversation agent's state for the next turn. Resolves on
   * acceptance; rejects with a safe Error message. Absent ⇒
   * `ui/update-model-context` is method-not-found.
   */
  updateModelContext?: (
    payload: UpdateModelContextParams,
  ) => Promise<void>;
  /**
   * Obtain user consent for an App-initiated action (PR #6, frontend-only
   * — no backend turn to pause). Resolves `true` to proceed. Absent ⇒ the
   * bridge keeps PR #4 behavior and opens links directly (back-compat for
   * older hosts / specs). Capability consent is handled host-side before
   * the frame renders, so the bridge only ever asks for `open-link`.
   */
  requestConsent?: (req: ConsentRequest) => Promise<boolean>;
  /**
   * Apply an App-initiated `ui/request-display-mode` change (e.g. expand to
   * fullscreen). The component owns the DOM, so it decides what it can
   * honor and returns the mode it actually applied — the spec requires the
   * host respond with the *resulting* mode, not the requested one. Absent
   * (older hosts / tests) ⇒ the host stays inline-only and every request
   * resolves to `inline`, advertising only `['inline']` at initialize.
   */
  requestDisplayMode?: (mode: DisplayMode) => DisplayMode;
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

  /** Whether the COMPLETE tool-input was sent (spec: at most once). Partials
   *  (`tool-input-partial`) may stream freely before this flips true. */
  private toolInputSent = false;

  /** Current display mode (host-owned; mirrored to the View on change). */
  private displayMode: DisplayMode = 'inline';

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
   * Whether the View has reported `initialized`. A paced partial-input relay
   * reads this so it doesn't push sends into `preInitQueue` (which flushes all
   * at once on initialize) — which would collapse the pacing back into a burst.
   */
  get viewIsInitialized(): boolean {
    return this.viewInitialized;
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

      case M_TOOLS_CALL: {
        if (!isRequest(msg)) return;
        const p = msg.params as
          | { name?: string; arguments?: Record<string, unknown> }
          | undefined;
        const name = p?.name;
        if (typeof name !== 'string' || !name) {
          this.respondError(msg.id, JSONRPC_IMPL_ERROR, 'Invalid tool name');
          return;
        }
        if (!this.d.proxyToolCall) {
          this.respondError(
            msg.id,
            JSONRPC_METHOD_NOT_FOUND,
            'tools/call not supported by this host',
          );
          return;
        }
        const reqId = msg.id;
        // app-api enforces auth + the conversation binding; inference-api
        // is the authoritative spec-MUST app-visibility gate. We forward
        // the View's CallToolResult back verbatim (content + isError).
        this.d
          .proxyToolCall(name, p?.arguments ?? {})
          .then((result) => this.respond(reqId, result))
          .catch((err: unknown) =>
            this.respondError(
              reqId,
              JSONRPC_IMPL_ERROR,
              err instanceof Error ? err.message : 'Tool call failed',
            ),
          );
        return;
      }

      case M_OPEN_LINK: {
        if (!isRequest(msg)) return;
        const url = (msg.params as { url?: string } | undefined)?.url;
        if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) {
          this.respondError(msg.id, JSONRPC_IMPL_ERROR, 'Invalid URL');
          return;
        }
        const reqId = msg.id;
        if (!this.d.requestConsent) {
          // No consent gate wired (older host / specs): PR #4 behavior.
          this.d.openLink(url);
          this.respond(reqId, {});
          return;
        }
        // PR #6: frontend-only consent. Hold the JSON-RPC response open
        // until the user answers the inline in-thread prompt.
        this.d
          .requestConsent({ kind: 'open-link', url })
          .then((granted) => {
            if (granted) {
              this.d.openLink(url);
              this.respond(reqId, {});
            } else {
              this.respondError(
                reqId,
                JSONRPC_IMPL_ERROR,
                'User declined to open the link',
              );
            }
          })
          .catch(() =>
            this.respondError(
              reqId,
              JSONRPC_IMPL_ERROR,
              'Consent could not be obtained',
            ),
          );
        return;
      }

      case M_REQUEST_DISPLAY_MODE: {
        if (!isRequest(msg)) return;
        const requested = (msg.params as RequestDisplayModeParams | undefined)
          ?.mode;
        // Spec: MUST return the *resulting* mode (the current one when the
        // request can't be honored). The component owns the DOM and decides;
        // this host supports inline + fullscreen (pip falls back to inline).
        const resulting: DisplayMode =
          this.d.requestDisplayMode &&
          (requested === 'fullscreen' || requested === 'inline')
            ? this.d.requestDisplayMode(requested)
            : 'inline';
        this.setDisplayMode(resulting);
        this.respond(msg.id, { mode: resulting });
        return;
      }

      case M_MESSAGE: {
        if (!isRequest(msg)) return;
        // Capability check before params: a host without the dep simply
        // doesn't support the method (older host / tests degrade per
        // JSON-RPC, regardless of payload).
        if (!this.d.sendMessage) {
          this.respondError(
            msg.id,
            JSONRPC_METHOD_NOT_FOUND,
            'ui/message not supported by this host',
          );
          return;
        }
        const p = msg.params as Partial<MessageParams> | undefined;
        // `content` is an ARRAY of content blocks per SEP-1865 / the ext-apps
        // SDK; concatenate the text blocks into the relayed user turn (mirrors
        // the ui/update-model-context handler's array handling below).
        const blocks = Array.isArray(p?.content) ? p!.content : [];
        const text =
          p?.role === 'user'
            ? blocks
                .filter((b) => b?.type === 'text' && typeof b?.text === 'string')
                .map((b) => b!.text as string)
                .join('\n')
                .trim()
            : '';
        if (!text) {
          this.respondError(
            msg.id,
            JSONRPC_IMPL_ERROR,
            'Invalid ui/message params',
          );
          return;
        }
        const reqId = msg.id;
        // Treated identically to a typed message: a real streaming turn.
        this.d
          .sendMessage(text)
          .then(() => this.respond(reqId, {}))
          .catch((err: unknown) =>
            this.respondError(
              reqId,
              JSONRPC_IMPL_ERROR,
              err instanceof Error ? err.message : 'Failed to send message',
            ),
          );
        return;
      }

      case M_UPDATE_MODEL_CONTEXT: {
        if (!isRequest(msg)) return;
        if (!this.d.updateModelContext) {
          this.respondError(
            msg.id,
            JSONRPC_METHOD_NOT_FOUND,
            'ui/update-model-context not supported by this host',
          );
          return;
        }
        const p = msg.params as UpdateModelContextParams | undefined;
        const hasContent =
          Array.isArray(p?.content) && p!.content!.length > 0;
        const hasStructured =
          !!p?.structuredContent &&
          typeof p.structuredContent === 'object';
        if (!hasContent && !hasStructured) {
          this.respondError(
            msg.id,
            JSONRPC_IMPL_ERROR,
            'Invalid ui/update-model-context params',
          );
          return;
        }
        const reqId = msg.id;
        this.d
          .updateModelContext({
            content: hasContent ? p!.content : undefined,
            structuredContent: hasStructured
              ? p!.structuredContent
              : undefined,
          })
          .then(() => this.respond(reqId, {}))
          .catch((err: unknown) =>
            this.respondError(
              reqId,
              JSONRPC_IMPL_ERROR,
              err instanceof Error
                ? err.message
                : 'Failed to update model context',
            ),
          );
        return;
      }

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
        // Only advertise serverTools when the host can actually proxy
        // (PR #5). Absent ⇒ the App won't attempt tools/call.
        ...(this.d.proxyToolCall ? { serverTools: {} } : {}),
        sandbox: {
          permissions: this.d.resource.permissions,
          csp: this.d.resource.csp,
        },
      },
      hostContext: {
        ...this.d.getHostContext(),
        displayMode: this.displayMode,
        availableDisplayModes: this.availableDisplayModes(),
      },
    });
  }

  /** Modes this host can switch to — fullscreen only when the dep is wired. */
  private availableDisplayModes(): DisplayMode[] {
    return this.d.requestDisplayMode ? ['inline', 'fullscreen'] : ['inline'];
  }

  /**
   * Record the resulting display mode and, on an actual change, tell the
   * View via `host-context-changed`. Covers both App-initiated requests
   * (via `ui/request-display-mode`) and host-initiated exits (the user
   * dismissing fullscreen — see `notifyDisplayMode`).
   */
  private setDisplayMode(mode: DisplayMode): void {
    if (mode === this.displayMode) return;
    this.displayMode = mode;
    this.notifyHostContextChanged({ displayMode: mode });
  }

  /** Host-initiated display-mode change (e.g. the user exits fullscreen). */
  notifyDisplayMode(mode: DisplayMode): void {
    this.setDisplayMode(mode);
  }

  // --- outbound -----------------------------------------------------------

  private sizeChangedCb: ((w: number, h: number) => void) | null = null;
  /** Register the size-changed sink (the component resizes the iframe). */
  onSizeChanged(cb: (w: number, h: number) => void): void {
    this.sizeChangedCb = cb;
  }

  private sendSandboxResourceReady(): void {
    // Reserved host→proxy notification; consumed by the proxy, not forwarded.
    // Inner-iframe sandbox matches the ext-apps basic-host reference
    // (`examples/basic-host/src/sandbox.ts`): allow-scripts + allow-same-origin
    // + allow-forms. allow-same-origin is required for proxy.js to populate
    // the inner doc via document.write and for typical App bundles
    // (Excalidraw, Cesium, etc.) to access localStorage at the sandbox origin.
    // The mcp-sandbox origin is a static CDN with no shared state, so this
    // does not weaken the cross-origin boundary against the SPA. Apps wanting
    // stricter isolation can opt into null-origin via `_meta.ui.sandbox` once
    // that pass-through lands.
    this.postToProxy({
      jsonrpc: '2.0',
      method: M_SANDBOX_RESOURCE_READY,
      params: {
        html: this.d.resource.html,
        sandbox: 'allow-scripts allow-same-origin allow-forms',
        csp: this.d.resource.csp,
        permissions: this.d.resource.permissions,
        nonce: this.d.nonce,
      },
    });
    // From here on every inbound message MUST carry the nonce.
    this.nonceArmed = true;
  }

  /**
   * Push tool input + result on `initialized`. If the input is still
   * streaming (`isToolInputFinal` present and false), send the latest healed
   * PARTIAL — the App renders progressively — and defer the complete
   * `tool-input` until the component calls `sendToolInputFinal`. Otherwise
   * (final, or no streaming wired) send the complete `tool-input` once. The
   * `tool-result` follows when available.
   */
  private pushToolData(): void {
    const final = this.d.isToolInputFinal?.() ?? true;
    if (final) {
      this.sendToolInputFinal();
    } else {
      const partial = this.d.getPartialToolInput?.();
      if (partial) this.sendToolInputPartial(partial);
    }
    const result = this.d.getToolResult();
    if (result != null) {
      this.sendNotification(M_TOOL_RESULT, result);
    }
  }

  /**
   * Relay a streamed partial tool-input (SEP-1865). No-op once the complete
   * `tool-input` has been sent — late partials must never clobber the final.
   */
  sendToolInputPartial(args: Record<string, unknown>): void {
    if (this.toolInputSent) return;
    this.sendNotification(M_TOOL_INPUT_PARTIAL, { arguments: args });
  }

  /** Send the complete `tool-input` exactly once (spec: at most one). */
  sendToolInputFinal(): void {
    if (this.toolInputSent) return;
    this.toolInputSent = true;
    this.sendNotification(M_TOOL_INPUT, { arguments: this.d.getToolInput() });
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
