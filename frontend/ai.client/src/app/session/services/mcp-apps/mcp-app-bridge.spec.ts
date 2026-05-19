import { describe, it, expect, beforeEach, vi } from 'vitest';
import { McpAppBridge } from './mcp-app-bridge';
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
  warn: ReturnType<typeof vi.fn>;
  toolResult: { value: unknown | null };
}

function makeBridge(): Harness {
  const host = new FakeHostWindow();
  const proxy = new FakeProxyWindow();
  const openLink = vi.fn();
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
    onWarn: warn,
  });
  bridge.start();
  return { bridge, host, proxy, openLink, warn, toolResult };
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
    expect(msg.params.sandbox).toBe('allow-scripts');
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

  it('answers ui/request-display-mode with the resulting mode', () => {
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
    expect(h.proxy.byId('d1').result).toEqual({
      mode: 'inline',
    });
  });

  it('answers PR #6 methods (ui/message, ui/update-model-context) with method-not-found', () => {
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
});
