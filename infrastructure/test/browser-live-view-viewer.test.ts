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
  post(url?: string): void;
  postFrom(origin: string, url?: string): void;
  status(): string;
  failAuth(): void;
  succeedAuth(): void;
}

function load(): Harness {
  const listeners: ((e: unknown) => void)[] = [];
  const authCalls: unknown[] = [];
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
    connect: () => new Promise(() => {}), // never settles: mid-connect is the window under test
  };

  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox);

  return {
    authCalls,
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
