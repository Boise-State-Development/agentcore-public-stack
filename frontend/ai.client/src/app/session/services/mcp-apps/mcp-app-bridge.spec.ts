import { describe, it, expect, beforeEach, vi } from 'vitest';
import { McpAppBridge } from './mcp-app-bridge';
import type { DisplayMode } from './mcp-app-protocol';
import type { UiResourceEvent } from '../../../shared/utils/stream-parser';

const SANDBOX_ORIGIN = 'https://mcp-sandbox.example.com';
const NONCE = 'nonce-abc';

function resource(): UiResourceEvent {
  return {
    type: 'ui_resource',
    toolUseId: 'tu-1',
    resourceUri: 'ui://srv/widget',
    html: '<h1>app</h1>',
    mimeType: 'text/html;profile=mcp-app',
    csp: { connectDomains: ['https://api.test'] },
    permissions: { clipboardWrite: {} },
    sandboxOrigin: SANDBOX_ORIGIN,
  };
}

/** A postMessage sink standing in for the proxy iframe's contentWindow. */
class FakeProxyWindow {
  readonly sent: Array<{ msg: any; targetOrigin: string }> = [];
  postMessage(msg: unknown, targetOrigin: string): void {
    this.sent.push({ msg, targetOrigin });
  }
  last(): any {
    return this.sent[this.sent.length - 1]?.msg;
  }
  byMethod(method: string): any[] {
    return this.sent.map((s) => s.msg).filter((m) => m?.method === method);
  }
  /** The message responding to/with the given JSON-RPC id, or throws. */
  byId(id: string | number): any {
    const found = this.sent.find((s) => s.msg?.id === id);
    if (!found) throw new Error(`no message with id ${id}`);
    return found.msg;
  }
}

/** Host window: lets the test deliver `message` events to the bridge. */
class FakeHostWindow {
  private listener: ((ev: MessageEvent) => void) | null = null;
  addEventListener(_t: 'message', cb: (ev: MessageEvent) => void): void {
    this.listener = cb;
  }
  removeEventListener(): void {
    this.listener = null;
  }
  get attached(): boolean {
    return this.listener !== null;
  }
  deliver(data: unknown, source: unknown, origin = SANDBOX_ORIGIN): void {
    this.listener?.({ data, source, origin } as MessageEvent);
  }
}

interface Harness {
  bridge: McpAppBridge;
  host: FakeHostWindow;
  proxy: FakeProxyWindow;
  openLink: ReturnType<typeof vi.fn>;
  proxyToolCall: ReturnType<typeof vi.fn>;
  sendMessage: ReturnType<typeof vi.fn>;
  updateModelContext: ReturnType<typeof vi.fn>;
  requestConsent: ReturnType<typeof vi.fn>;
  requestDisplayMode: ReturnType<typeof vi.fn>;
  warn: ReturnType<typeof vi.fn>;
  toolResult: { value: unknown | null };
}

function makeBridge(
  opts: { withProxy?: boolean; pr6?: boolean; displayMode?: boolean } = {},
): Harness {
  const host = new FakeHostWindow();
  const proxy = new FakeProxyWindow();
  const openLink = vi.fn();
  const proxyToolCall = vi.fn(async () => ({
    content: [{ type: 'text', text: 'tool-ok' }],
    isError: false,
  }));
  // PR #6 deps. Wired only when opts.pr6 — otherwise the bridge must
  // degrade per JSON-RPC (method-not-found / direct open) so older hosts
  // keep working.
  const sendMessage = vi.fn(async () => undefined);
  const updateModelContext = vi.fn(async () => undefined);
  const requestConsent = vi.fn(async () => true);
  // Echoes the requested mode (the real component clamps pip→inline; the
  // bridge already only forwards inline/fullscreen here).
  const requestDisplayMode = vi.fn((mode: DisplayMode): DisplayMode => mode);
  const warn = vi.fn();
  const toolResult: { value: unknown | null } = {
    value: { content: [{ type: 'text', text: 'ok' }], isError: false },
  };
  const bridge = new McpAppBridge({
    hostWindow: host,
    getProxyWindow: () => proxy as unknown as Window,
    sandboxOrigin: SANDBOX_ORIGIN,
    resource: resource(),
    nonce: NONCE,
    getToolInput: () => ({ q: 'paris' }),
    getToolResult: () => toolResult.value,
    getHostContext: () => ({ theme: 'dark' }),
    openLink,
    // Default harness can proxy; opt out to assert the no-capability path.
    ...(opts.withProxy === false ? {} : { proxyToolCall }),
    ...(opts.pr6 ? { sendMessage, updateModelContext, requestConsent } : {}),
    ...(opts.displayMode ? { requestDisplayMode } : {}),
    onWarn: warn,
  });
  bridge.start();
  return {
    bridge,
    host,
    proxy,
    openLink,
    proxyToolCall,
    sendMessage,
    updateModelContext,
    requestConsent,
    requestDisplayMode,
    warn,
    toolResult,
  };
}

/** Drive the handshake up to (but not including) `initialized`. */
function handshake(h: Harness): void {
  h.host.deliver(
    { jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} },
    h.proxy,
  );
}

describe('McpAppBridge', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeBridge();
  });

  it('sends sandbox-resource-ready (with nonce + html + csp) on proxy-ready', () => {
    handshake(h);
    const msg = h.proxy.byMethod('ui/notifications/sandbox-resource-ready')[0];
    expect(msg).toBeTruthy();
    expect(msg.params.html).toBe('<h1>app</h1>');
    expect(msg.params.nonce).toBe(NONCE);
    expect(msg.params.csp).toEqual({ connectDomains: ['https://api.test'] });
    expect(msg.params.permissions).toEqual({ clipboardWrite: {} });
    expect(msg.params.sandbox).toBe('allow-scripts allow-same-origin allow-forms');
    // Strict targetOrigin — never '*'.
    expect(h.proxy.sent[0].targetOrigin).toBe(SANDBOX_ORIGIN);
  });

  it('rejects messages from the wrong source or origin', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 1, method: 'ping', nonce: NONCE },
      {} /* not the proxy window */,
    );
    h.host.deliver(
      { jsonrpc: '2.0', id: 2, method: 'ping', nonce: NONCE },
      h.proxy,
      'https://evil.example.com',
    );
    // No ping response went out for either rejected message.
    expect(h.proxy.sent.some((s) => s.msg.id === 1 || s.msg.id === 2)).toBe(false);
  });

  it('drops post-handshake messages without the nonce', () => {
    handshake(h);
    h.host.deliver({ jsonrpc: '2.0', id: 9, method: 'ping' }, h.proxy);
    expect(h.warn).toHaveBeenCalled();
    expect(h.proxy.sent.some((s) => s.msg.id === 9)).toBe(false);
  });

  it('answers ui/initialize with protocol version + host capabilities', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 'i1', method: 'ui/initialize', nonce: NONCE, params: {} },
      h.proxy,
    );
    const resp = h.proxy.sent.map((s) => s.msg).find((m) => m.id === 'i1');
    expect(resp.result.protocolVersion).toBe('2026-01-26');
    expect(resp.result.hostCapabilities.openLinks).toBeDefined();
    expect(resp.result.hostCapabilities.sandbox.csp).toEqual({
      connectDomains: ['https://api.test'],
    });
    expect(resp.result.hostContext.displayMode).toBe('inline');
    expect(resp.result.hostContext.theme).toBe('dark');
  });

  it('does NOT push tool-input before initialized, then flushes on initialized', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 'i1', method: 'ui/initialize', nonce: NONCE, params: {} },
      h.proxy,
    );
    expect(h.proxy.byMethod('ui/notifications/tool-input')).toHaveLength(0);

    h.host.deliver(
      { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
      h.proxy,
    );
    const input = h.proxy.byMethod('ui/notifications/tool-input');
    const result = h.proxy.byMethod('ui/notifications/tool-result');
    expect(input).toHaveLength(1);
    expect(input[0].params).toEqual({ arguments: { q: 'paris' } });
    expect(result).toHaveLength(1);
    expect(result[0].params).toEqual({
      content: [{ type: 'text', text: 'ok' }],
      isError: false,
    });
    // tool-input MUST precede tool-result.
    const order = h.proxy.sent
      .map((s) => s.msg.method)
      .filter((m) => m && m.startsWith('ui/notifications/tool-'));
    expect(order.indexOf('ui/notifications/tool-input')).toBeLessThan(
      order.indexOf('ui/notifications/tool-result'),
    );
  });

  it('forwards size-changed to the registered sink', () => {
    const sizes: Array<[number, number]> = [];
    h.bridge.onSizeChanged((w, ht) => sizes.push([w, ht]));
    handshake(h);
    h.host.deliver(
      {
        jsonrpc: '2.0',
        method: 'ui/notifications/size-changed',
        nonce: NONCE,
        params: { width: 320, height: 540 },
      },
      h.proxy,
    );
    expect(sizes).toEqual([[320, 540]]);
  });

  it('handles ui/open-link: opens valid https, rejects bad URLs', () => {
    handshake(h);
    h.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'l1',
        method: 'ui/open-link',
        nonce: NONCE,
        params: { url: 'https://example.com/x' },
      },
      h.proxy,
    );
    expect(h.openLink).toHaveBeenCalledWith('https://example.com/x');
    expect(h.proxy.byId('l1').result).toEqual({});

    h.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'l2',
        method: 'ui/open-link',
        nonce: NONCE,
        params: { url: 'javascript:alert(1)' },
      },
      h.proxy,
    );
    expect(h.openLink).toHaveBeenCalledTimes(1);
    expect(h.proxy.byId('l2').error.code).toBe(-32000);
  });

  it('answers ui/request-display-mode with inline when the host lacks the dep', () => {
    handshake(h);
    h.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'd1',
        method: 'ui/request-display-mode',
        nonce: NONCE,
        params: { mode: 'fullscreen' },
      },
      h.proxy,
    );
    expect(h.proxy.byId('d1').result).toEqual({ mode: 'inline' });
    // Mode never changed → no host-context-changed churn toward the View.
    expect(h.proxy.byMethod('ui/notifications/host-context-changed')).toHaveLength(0);
  });

  describe('with the requestDisplayMode dep wired', () => {
    beforeEach(() => {
      h = makeBridge({ displayMode: true });
    });

    it('advertises inline + fullscreen at initialize', () => {
      handshake(h);
      h.host.deliver(
        { jsonrpc: '2.0', id: 'i1', method: 'ui/initialize', nonce: NONCE, params: {} },
        h.proxy,
      );
      const resp = h.proxy.byId('i1');
      expect(resp.result.hostContext.displayMode).toBe('inline');
      expect(resp.result.hostContext.availableDisplayModes).toEqual([
        'inline',
        'fullscreen',
      ]);
    });

    it('honors a fullscreen request and mirrors it via host-context-changed', () => {
      handshake(h);
      // initialized so the host-context-changed notification flushes.
      h.host.deliver(
        { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
        h.proxy,
      );
      h.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'd1',
          method: 'ui/request-display-mode',
          nonce: NONCE,
          params: { mode: 'fullscreen' },
        },
        h.proxy,
      );
      expect(h.requestDisplayMode).toHaveBeenCalledWith('fullscreen');
      expect(h.proxy.byId('d1').result).toEqual({ mode: 'fullscreen' });
      const changes = h.proxy.byMethod('ui/notifications/host-context-changed');
      expect(changes.at(-1).params).toEqual({ displayMode: 'fullscreen' });
    });

    it('exposes host-initiated exit via notifyDisplayMode', () => {
      handshake(h);
      h.host.deliver(
        { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
        h.proxy,
      );
      // Enter fullscreen, then the user dismisses it host-side.
      h.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'd1',
          method: 'ui/request-display-mode',
          nonce: NONCE,
          params: { mode: 'fullscreen' },
        },
        h.proxy,
      );
      h.bridge.notifyDisplayMode('inline');
      const changes = h.proxy.byMethod('ui/notifications/host-context-changed');
      expect(changes.at(-1).params).toEqual({ displayMode: 'inline' });
    });
  });

  it('degrades ui/message + ui/update-model-context to method-not-found when the host lacks the deps', () => {
    // Default harness wires no PR #6 deps → an older host that doesn't
    // support these methods. Per JSON-RPC the App should get -32601 and
    // fall back, regardless of payload.
    handshake(h);
    for (const [id, method] of [
      ['m1', 'ui/message'],
      ['m2', 'ui/update-model-context'],
    ] as const) {
      h.host.deliver(
        { jsonrpc: '2.0', id, method, nonce: NONCE, params: {} },
        h.proxy,
      );
      expect(h.proxy.byId(id).error.code).toBe(-32601);
    }
  });

  describe('PR #6 — implemented (deps wired)', () => {
    let p: Harness;
    beforeEach(() => {
      p = makeBridge({ pr6: true });
      handshake(p);
    });

    it('ui/message relays a valid user text as a real turn', async () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'm1',
          method: 'ui/message',
          nonce: NONCE,
          params: { role: 'user', content: [{ type: 'text', text: '  hi  ' }] },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      expect(p.sendMessage).toHaveBeenCalledWith('hi');
      expect(p.proxy.byId('m1').result).toEqual({});
    });

    it('ui/message concatenates multiple text blocks in the array', async () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'm1b',
          method: 'ui/message',
          nonce: NONCE,
          params: {
            role: 'user',
            content: [
              { type: 'text', text: 'line one' },
              { type: 'text', text: 'line two' },
            ],
          },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      expect(p.sendMessage).toHaveBeenCalledWith('line one\nline two');
      expect(p.proxy.byId('m1b').result).toEqual({});
    });

    it('ui/message with bad params is invalid-params (-32000), not relayed', () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'm2',
          method: 'ui/message',
          nonce: NONCE,
          params: { role: 'assistant' },
        },
        p.proxy,
      );
      expect(p.sendMessage).not.toHaveBeenCalled();
      expect(p.proxy.byId('m2').error.code).toBe(-32000);
    });

    it('ui/update-model-context relays structuredContent', async () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'c1',
          method: 'ui/update-model-context',
          nonce: NONCE,
          params: { structuredContent: { picked: 'X' } },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      expect(p.updateModelContext).toHaveBeenCalledWith({
        content: undefined,
        structuredContent: { picked: 'X' },
      });
      expect(p.proxy.byId('c1').result).toEqual({});
    });

    it('ui/update-model-context with neither content nor structured is -32000', () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'c2',
          method: 'ui/update-model-context',
          nonce: NONCE,
          params: {},
        },
        p.proxy,
      );
      expect(p.updateModelContext).not.toHaveBeenCalled();
      expect(p.proxy.byId('c2').error.code).toBe(-32000);
    });

    it('ui/open-link asks for consent and opens only when granted', async () => {
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'l1',
          method: 'ui/open-link',
          nonce: NONCE,
          params: { url: 'https://example.com/x' },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      expect(p.requestConsent).toHaveBeenCalledWith({
        kind: 'open-link',
        url: 'https://example.com/x',
      });
      expect(p.openLink).toHaveBeenCalledWith('https://example.com/x');
      expect(p.proxy.byId('l1').result).toEqual({});
    });

    it('ui/open-link denied → not opened, JSON-RPC error', async () => {
      p.requestConsent.mockResolvedValueOnce(false);
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'l2',
          method: 'ui/open-link',
          nonce: NONCE,
          params: { url: 'https://example.com/y' },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      expect(p.openLink).not.toHaveBeenCalled();
      expect(p.proxy.byId('l2').error.code).toBe(-32000);
    });

    it('a rejected sendMessage surfaces a JSON-RPC error', async () => {
      p.sendMessage.mockRejectedValueOnce(new Error('quota exceeded'));
      p.host.deliver(
        {
          jsonrpc: '2.0',
          id: 'm3',
          method: 'ui/message',
          nonce: NONCE,
          params: { role: 'user', content: [{ type: 'text', text: 'go' }] },
        },
        p.proxy,
      );
      await Promise.resolve();
      await Promise.resolve();
      const err = p.proxy.byId('m3').error;
      expect(err.code).toBe(-32000);
      expect(err.message).toBe('quota exceeded');
    });
  });

  it('answers ping with an empty result', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 'p1', method: 'ping', nonce: NONCE },
      h.proxy,
    );
    expect(h.proxy.byId('p1').result).toEqual({});
  });

  it('dispose() sends resource-teardown after init and detaches the listener', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
      h.proxy,
    );
    h.bridge.dispose('bye');
    const td = h.proxy.byMethod('ui/resource-teardown');
    expect(td).toHaveLength(1);
    expect(td[0].params).toEqual({ reason: 'bye' });
    expect(h.host.attached).toBe(false);
  });

  it('queues host-context-changed before init and flushes after', () => {
    handshake(h);
    h.bridge.notifyHostContextChanged({ theme: 'light' });
    expect(h.proxy.byMethod('ui/notifications/host-context-changed')).toHaveLength(0);
    h.host.deliver(
      { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
      h.proxy,
    );
    const hc = h.proxy.byMethod('ui/notifications/host-context-changed');
    expect(hc).toHaveLength(1);
    expect(hc[0].params).toEqual({ theme: 'light' });
  });

  // ── PR #5: app-initiated tools/call proxying ───────────────────────────

  it('advertises serverTools only when a proxy is available', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 'i1', method: 'ui/initialize', nonce: NONCE, params: {} },
      h.proxy,
    );
    expect(h.proxy.byId('i1').result.hostCapabilities.serverTools).toBeDefined();

    const noProxy = makeBridge({ withProxy: false });
    handshake(noProxy);
    noProxy.host.deliver(
      { jsonrpc: '2.0', id: 'i2', method: 'ui/initialize', nonce: NONCE, params: {} },
      noProxy.proxy,
    );
    expect(
      noProxy.proxy.byId('i2').result.hostCapabilities.serverTools,
    ).toBeUndefined();
  });

  it('proxies a tools/call and returns the CallToolResult to the View', async () => {
    handshake(h);
    h.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'c1',
        method: 'tools/call',
        nonce: NONCE,
        params: { name: 'widget_tool', arguments: { q: 'x' } },
      },
      h.proxy,
    );
    expect(h.proxyToolCall).toHaveBeenCalledWith('widget_tool', { q: 'x' });
    // proxyToolCall is async — let the microtask settle.
    await Promise.resolve();
    await Promise.resolve();
    expect(h.proxy.byId('c1').result).toEqual({
      content: [{ type: 'text', text: 'tool-ok' }],
      isError: false,
    });
  });

  it('answers tools/call with an error when the proxy rejects', async () => {
    h.proxyToolCall.mockRejectedValueOnce(new Error('not app-visible'));
    handshake(h);
    h.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'c2',
        method: 'tools/call',
        nonce: NONCE,
        params: { name: 'blocked', arguments: {} },
      },
      h.proxy,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(h.proxy.byId('c2').error.message).toBe('not app-visible');
  });

  it('rejects tools/call with no tool name', () => {
    handshake(h);
    h.host.deliver(
      { jsonrpc: '2.0', id: 'c3', method: 'tools/call', nonce: NONCE, params: {} },
      h.proxy,
    );
    expect(h.proxy.byId('c3').error.code).toBe(-32000);
    expect(h.proxyToolCall).not.toHaveBeenCalled();
  });

  it('answers tools/call method-not-found when the host cannot proxy', () => {
    const np = makeBridge({ withProxy: false });
    handshake(np);
    np.host.deliver(
      {
        jsonrpc: '2.0',
        id: 'c4',
        method: 'tools/call',
        nonce: NONCE,
        params: { name: 'widget_tool' },
      },
      np.proxy,
    );
    expect(np.proxy.byId('c4').error.code).toBe(-32601);
  });
});

describe('McpAppBridge — streamed partial tool input (SEP-1865)', () => {
  // Controllable streaming state: `final` flips when the input is complete.
  function makeStreamingBridge(initial: {
    partial: Record<string, unknown> | null;
    final: boolean;
  }) {
    const host = new FakeHostWindow();
    const proxy = new FakeProxyWindow();
    const state = { ...initial };
    const bridge = new McpAppBridge({
      hostWindow: host,
      getProxyWindow: () => proxy as unknown as Window,
      sandboxOrigin: SANDBOX_ORIGIN,
      resource: resource(),
      nonce: NONCE,
      getToolInput: () => ({ elements: [{ type: 'rect' }, { type: 'cam' }] }),
      getPartialToolInput: () => state.partial,
      isToolInputFinal: () => state.final,
      getToolResult: () => null,
      getHostContext: () => ({ theme: 'dark' }),
      openLink: vi.fn(),
    });
    bridge.start();
    return { bridge, host, proxy, state };
  }

  function toInitialized(h: { host: FakeHostWindow; proxy: FakeProxyWindow }) {
    h.host.deliver(
      { jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} },
      h.proxy,
    );
    h.host.deliver(
      { jsonrpc: '2.0', id: 'i1', method: 'ui/initialize', nonce: NONCE, params: {} },
      h.proxy,
    );
    h.host.deliver(
      { jsonrpc: '2.0', method: 'ui/notifications/initialized', nonce: NONCE },
      h.proxy,
    );
  }

  const PARTIAL = 'ui/notifications/tool-input-partial';
  const FINAL = 'ui/notifications/tool-input';

  it('on initialized while streaming, sends the partial and NOT the final', () => {
    const h = makeStreamingBridge({ partial: { elements: [{ type: 'rect' }] }, final: false });
    toInitialized(h);
    const partials = h.proxy.byMethod(PARTIAL);
    expect(partials).toHaveLength(1);
    expect(partials[0].params).toEqual({ arguments: { elements: [{ type: 'rect' }] } });
    expect(h.proxy.byMethod(FINAL)).toHaveLength(0);
  });

  it('on initialized when already final, sends the complete tool-input', () => {
    const h = makeStreamingBridge({ partial: null, final: true });
    toInitialized(h);
    expect(h.proxy.byMethod(PARTIAL)).toHaveLength(0);
    const finals = h.proxy.byMethod(FINAL);
    expect(finals).toHaveLength(1);
    expect(finals[0].params).toEqual({
      arguments: { elements: [{ type: 'rect' }, { type: 'cam' }] },
    });
  });

  it('streams further partials, then the final exactly once; late partials are ignored', () => {
    const h = makeStreamingBridge({ partial: { elements: [{ type: 'rect' }] }, final: false });
    toInitialized(h);

    h.bridge.sendToolInputPartial({ elements: [{ type: 'rect' }, { type: 'cam' }] });
    expect(h.proxy.byMethod(PARTIAL)).toHaveLength(2);

    // Input completes → final goes out once.
    h.bridge.sendToolInputFinal();
    expect(h.proxy.byMethod(FINAL)).toHaveLength(1);
    h.bridge.sendToolInputFinal();
    expect(h.proxy.byMethod(FINAL)).toHaveLength(1);

    // A late partial after the final must not clobber it.
    h.bridge.sendToolInputPartial({ elements: [] });
    expect(h.proxy.byMethod(PARTIAL)).toHaveLength(2);
  });

  it('queues a pre-initialized partial and flushes it on initialized', () => {
    const h = makeStreamingBridge({ partial: null, final: false });
    // Before initialized: a partial should queue, not post.
    h.bridge.sendToolInputPartial({ elements: [{ type: 'rect' }] });
    expect(h.proxy.byMethod(PARTIAL)).toHaveLength(0);
    toInitialized(h);
    expect(h.proxy.byMethod(PARTIAL).length).toBeGreaterThanOrEqual(1);
  });
});
