/**
 * The MCP Apps sandbox proxy mirrors the host theme onto a `dark` class at
 * the App document's root (`assets/mcp-sandbox/proxy.js`).
 *
 * Atlassian's Rovo widgets keep a light palette in `:root` and switch to dark
 * only under `html.dark`; they read `hostContext.theme` but never set that
 * class, so in a dark-mode host they painted white cards. Tailwind's
 * `darkMode: 'class'` Apps behave the same way.
 *
 * `proxy.js` is a browser IIFE and this suite runs in Node, so it runs the
 * script in a `vm` context against the few DOM members it touches.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as vm from 'vm';

const PROXY_SRC = fs.readFileSync(
  path.join(__dirname, '../assets/mcp-sandbox/proxy.js'),
  'utf8',
);

const NONCE = 'nonce-1';
const HOST_ORIGIN = 'https://host.example';

class FakeClassList {
  private readonly names = new Set<string>();
  toggle(name: string, force: boolean): void {
    if (force) this.names.add(name);
    else this.names.delete(name);
  }
  contains(name: string): boolean {
    return this.names.has(name);
  }
}

interface Sandbox {
  dispatchFromHost(data: Record<string, unknown>): void;
  appRoot(): { classList: FakeClassList } | null;
}

/** Load proxy.js against a fake DOM. `crossOrigin` makes the inner frame's
 *  document unreachable, as for a null-origin App (srcdoc fallback). */
function loadProxy(opts: { crossOrigin?: boolean } = {}): Sandbox {
  let listener: ((event: unknown) => void) | null = null;
  let root: { classList: FakeClassList } | null = null;

  const innerDoc = {
    documentElement: null as { classList: FakeClassList } | null,
    open() {
      this.documentElement = null;
    },
    write() {
      root = { classList: new FakeClassList() };
    },
    close() {
      this.documentElement = root;
    },
  };
  const innerWindow = { postMessage: jest.fn(), document: innerDoc };
  const inner = {
    style: {} as Record<string, string>,
    setAttribute: jest.fn(),
    addEventListener: jest.fn(),
    get contentDocument() {
      if (opts.crossOrigin) throw new Error('cross-origin');
      return innerDoc;
    },
    contentWindow: opts.crossOrigin ? { postMessage: jest.fn() } : innerWindow,
  };
  const hostWindow = { postMessage: jest.fn() };
  const document = {
    documentElement: { style: {} },
    body: { style: {}, appendChild: jest.fn() },
    createElement: () => inner,
  };
  const window = {
    parent: hostWindow,
    addEventListener: (_type: string, fn: (event: unknown) => void) => {
      listener = fn;
    },
  };

  vm.runInNewContext(PROXY_SRC, { window, document });

  return {
    dispatchFromHost(data) {
      listener?.({ source: hostWindow, origin: HOST_ORIGIN, data: { jsonrpc: '2.0', ...data } });
    },
    appRoot: () => root,
  };
}

function mount(sandbox: Sandbox, theme?: string): void {
  sandbox.dispatchFromHost({
    method: 'ui/notifications/sandbox-resource-ready',
    params: { html: '<div id="app"></div>', nonce: NONCE, ...(theme ? { theme } : {}) },
  });
}

describe('mcp-sandbox proxy.js: host theme -> App root `dark` class', () => {
  test('a dark host marks the App root dark at mount, before the App renders', () => {
    const sandbox = loadProxy();
    mount(sandbox, 'dark');
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(true);
  });

  test('a light host leaves the App root without the class', () => {
    const sandbox = loadProxy();
    mount(sandbox, 'light');
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(false);
  });

  test('follows the theme the host sends the View: ui/initialize, then host-context-changed', () => {
    const sandbox = loadProxy();
    mount(sandbox); // an older host that sends no theme at mount
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(false);

    sandbox.dispatchFromHost({ id: 0, nonce: NONCE, result: { hostContext: { theme: 'dark' } } });
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(true);

    sandbox.dispatchFromHost({
      method: 'ui/notifications/host-context-changed',
      nonce: NONCE,
      params: { theme: 'light' },
    });
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(false);
  });

  test('ignores a theme on a message without the frame nonce', () => {
    const sandbox = loadProxy();
    mount(sandbox, 'light');
    sandbox.dispatchFromHost({
      method: 'ui/notifications/host-context-changed',
      nonce: 'wrong',
      params: { theme: 'dark' },
    });
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(false);
  });

  test('ignores a value that is not a theme', () => {
    const sandbox = loadProxy();
    mount(sandbox, 'dark');
    sandbox.dispatchFromHost({
      method: 'ui/notifications/host-context-changed',
      nonce: NONCE,
      params: { theme: 'system' },
    });
    expect(sandbox.appRoot()?.classList.contains('dark')).toBe(true);
  });

  test('leaves a null-origin App alone instead of throwing', () => {
    const sandbox = loadProxy({ crossOrigin: true });
    expect(() => {
      mount(sandbox, 'dark');
      sandbox.dispatchFromHost({
        method: 'ui/notifications/host-context-changed',
        nonce: NONCE,
        params: { theme: 'light' },
      });
    }).not.toThrow();
  });
});
