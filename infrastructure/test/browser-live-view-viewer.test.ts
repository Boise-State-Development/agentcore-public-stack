/**
 * The live-view viewer (`assets/mcp-sandbox/live-view.js`) runs the real file
 * in a `vm` context against a minimal DOM stub. No jsdom: the script touches
 * only `document.getElementById`, `document.referrer`, `window.parent` and
 * `addEventListener`, so a stub is both sufficient and honest about what the
 * page depends on.
 *
 * What this guards is a defect measured on deployed dev: the SPA posts the
 * minted URL up to THREE times (after minting, on the iframe's `load`, and in
 * reply to the viewer's `ready`) because it cannot know which fires first.
 * The viewer originally guarded on an established `connection`, which is
 * assigned only after `dcv.connect()` resolves — so two posts milliseconds
 * apart both called `dcv.authenticate`, the second socket's open closed the
 * first, and the stream died with `Close received after close` / auth code 10.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as vm from 'vm';

const SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'assets', 'mcp-sandbox', 'live-view.js'),
  'utf8',
);

const PARENT = 'https://app.example.test';
const SIGNED = 'https://bedrock-agentcore.us-west-2.amazonaws.com/live-view?X-Amz-Signature=abc';

interface Harness {
  authCalls: unknown[];
  connectCalls: any[];
  post(url?: string): void;
  postFrom(origin: string, url?: string): void;
  fireAuthError(): void;
  extraSearchParams(): URLSearchParams;
  status(): string;
  failAuth(): void;
  succeedAuth(): void;
}

function load(): Harness {
  const listeners: ((e: unknown) => void)[] = [];
  const authCalls: unknown[] = [];
  const connectCalls: any[] = [];
  const statusEl = { textContent: '', hidden: false };
  let authConfig: any = null;

  const sandbox: any = {
    URL,
    URLSearchParams,
    console: { error() {}, info() {} },
    document: {
      referrer: `${PARENT}/thread`,
      getElementById: (id: string) => (id === 'status' ? statusEl : { }),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.parent = { postMessage() {} };
  sandbox.addEventListener = (type: string, fn: (e: unknown) => void) => {
    if (type === 'message') listeners.push(fn);
  };
  sandbox.dcv = {
    LogLevel: { WARN: 'warn' },
    setLogLevel() {},
    authenticate(url: string, cfg: any) {
      authCalls.push(url);
      authConfig = cfg;
    },
    connect(cfg: any) {
      connectCalls.push(cfg);
      return new Promise(() => {}); // never settles: mid-connect is the window under test
    },
  };

  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);

  return {
    authCalls,
    connectCalls,
    post(url = SIGNED) {
      this.postFrom(PARENT, url);
    },
    postFrom(origin: string, url = SIGNED) {
      const event = {
        origin,
        data: { type: 'browser-live-view/connect', url, viewport: { width: 1280, height: 800 } },
      };
      listeners.forEach((fn) => fn(event));
    },
    status: () => (statusEl.hidden ? '' : statusEl.textContent),
    fireAuthError: () => authConfig.error({}, { code: 10 }),
    extraSearchParams: () => authConfig.httpExtraSearchParams() as URLSearchParams,
    failAuth: () => authConfig.error({}, { code: 10 }),
    succeedAuth: () => authConfig.success({}, [{ sessionId: 's', authToken: 't' }]),
  };
}

describe('browser live-view viewer', () => {
  it('authenticates once even when the parent posts three times', () => {
    const h = load();

    h.post();
    h.post();
    h.post();

    // Three posts, one stream. A second `authenticate` here is the exact
    // defect that killed the session on dev.
    expect(h.authCalls).toHaveLength(1);
  });

  it('still only authenticates once while the first connect is in flight', () => {
    const h = load();

    h.post();
    h.succeedAuth(); // connect() called, promise deliberately never resolves
    h.post();

    expect(h.authCalls).toHaveLength(1);
  });

  it('lets the user retry after a failed attempt rather than latching shut', () => {
    const h = load();

    h.post();
    h.failAuth();
    h.post();

    // The latch must release on every terminal outcome, or one bad mint makes
    // the viewer permanently dead for that session.
    expect(h.authCalls).toHaveLength(2);
  });

  it('hands the SDK a URL with NO query string', () => {
    const h = load();

    h.post();

    // The SDK APPENDS `httpExtraSearchParams` to whatever URL it is given.
    // Passing the signed URL with its query still attached sends every SigV4
    // parameter twice, which the service answers with 403 — measured. That is
    // why the viewer never streamed on dev.
    expect(h.authCalls).toHaveLength(1);
    expect(String(h.authCalls[0])).not.toContain('?');
    expect(String(h.authCalls[0])).not.toContain('X-Amz-Signature');
  });

  it('still forwards the signature through httpExtraSearchParams', () => {
    const h = load();

    h.post();

    // Stripping the query must not mean dropping it: the transport reads it
    // from this callback when it builds the socket URI.
    const params = h.extraSearchParams();
    expect(params.get('X-Amz-Signature')).toBe('abc');
  });

  it('puts httpExtraSearchParams in observers, where connect actually reads it', () => {
    const h = load();
    h.post();
    h.succeedAuth();

    // Measured against the shipped bundle: at the TOP LEVEL this callback is
    // silently ignored by `connect`, and the stream socket opens with no query
    // at all — unsigned, and refused by the service. Inside `observers` it is
    // honoured. (`authenticate` is the opposite and reads it top-level.)
    const cfg = h.connectCalls[0];
    expect(typeof cfg.observers?.httpExtraSearchParams).toBe('function');
    expect(cfg.httpExtraSearchParams).toBeUndefined();

    // Observers or callbacks, never both — the SDK ignores one if both appear.
    expect(cfg.callbacks).toBeUndefined();
    expect(typeof cfg.observers.firstFrame).toBe('function');
    expect(typeof cfg.observers.disconnect).toBe('function');
  });

  it('sizes the display only once the first frame has arrived', () => {
    const h = load();
    h.post();
    h.succeedAuth();

    const cfg = h.connectCalls[0];
    const conn = {
      requestDisplayLayout: jest.fn((_layout: any[]) => Promise.resolve()),
    };

    // Nothing may be requested before the display channel exists: calling it
    // when the connect promise resolves rejects with "Display channel is not
    // available", and since it returns a PROMISE a try/catch never sees it —
    // it lands as an unhandled rejection. Measured on dev.
    expect(conn.requestDisplayLayout).not.toHaveBeenCalled();

    cfg.observers.firstFrame(conn);

    expect(conn.requestDisplayLayout).toHaveBeenCalledTimes(1);
    const layout = conn.requestDisplayLayout.mock.calls[0][0];
    expect(layout[0].rect).toEqual({ x: 0, y: 0, width: 1280, height: 800 });
  });

  it('swallows a rejected display-layout request rather than leaking it', async () => {
    const h = load();
    h.post();
    h.succeedAuth();

    const conn = {
      requestDisplayLayout: () => Promise.reject(new Error('Display channel is not available')),
    };

    // The stream is still usable at whatever size the server chose, so this
    // must not become an unhandled rejection.
    expect(() => h.connectCalls[0].observers.firstFrame(conn)).not.toThrow();
    await new Promise((r) => setTimeout(r, 0));
  });

  it('does not report failure for an auth error that lands AFTER success', () => {
    const h = load();
    h.post();
    h.succeedAuth();
    h.connectCalls[0].observers.firstFrame({});

    // The SDK closes the auth socket once it is done and reports that close
    // through `error` even though authentication SUCCEEDED — measured on dev,
    // where `connect` is already running when code 10 arrives and the stream
    // then establishes normally. #status is an absolutely-positioned overlay,
    // so surfacing this would paint an error across a working stream.
    h.fireAuthError();

    expect(h.status()).toBe('');
  });

  it('still reports failure when auth fails before any session exists', () => {
    const h = load();
    h.post();

    h.fireAuthError();

    expect(h.status()).toContain('Could not start the session');
  });

  it('ignores a connect message from any other origin', () => {
    const h = load();

    // A sibling frame must not be able to drive this viewer onto a stream of
    // its choosing — this is the page where a password gets typed.
    h.postFrom('https://evil.example');

    expect(h.authCalls).toHaveLength(0);
  });

  it('refuses to render when it was not framed', () => {
    const listeners: ((e: unknown) => void)[] = [];
    const statusEl = { textContent: '', hidden: false };
    const sandbox: any = {
      URL, URLSearchParams, console: { error() {}, info() {} },
      document: { referrer: '', getElementById: () => statusEl },
    };
    sandbox.window = sandbox;
    sandbox.window.parent = sandbox; // top-level: parent === window
    sandbox.addEventListener = (t: string, fn: (e: unknown) => void) => {
      if (t === 'message') listeners.push(fn);
    };
    sandbox.dcv = { LogLevel: { WARN: 'w' }, setLogLevel() {}, authenticate() { throw new Error('must not authenticate'); } };
    vm.createContext(sandbox);
    vm.runInContext(SOURCE, sandbox);

    expect(statusEl.textContent).toContain('must be opened from the app');
    expect(listeners).toHaveLength(0);
  });
});
