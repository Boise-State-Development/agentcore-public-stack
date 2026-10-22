// Loads the JIT compiler before any spec runs.
//
// Angular packages ship fesm2022 chunks with partial declarations
// (ɵɵngDeclareInjectable / ɵɵngDeclareFactory) that compile EAGERLY at module
// evaluation. The unit-test builder keeps node_modules external
// (externalPackages: true), so those chunks reach the vitest module runner
// unlinked and fall back to JIT. Normally `@angular/core/testing` (imported by
// the builder's init-testbed setup) transitively evaluates `@angular/compiler`
// first, but that ordering is incidental — specs with no static Angular imports
// (e.g. app.spec.ts's dynamic `import('./app')`) can evaluate a raw
// `@angular/common` chunk before the compiler is present and die with
// "The injectable 'PlatformLocation' needs to be compiled using the JIT
// compiler, but '@angular/compiler' is not available". This import makes the
// compiler's presence an explicit invariant. See angular/angular-cli#31993.
import '@angular/compiler';

// Per-test teardown of process-global state.
//
// The unit-test builder runs vitest with `isolate: false` (hardcoded in
// @angular/build's vitest runner), so every spec file assigned to a worker
// shares one module registry, one jsdom, and one timer implementation. Worker
// assignment is timing-dependent, so a spec that leaves a global mutated
// breaks whichever unrelated spec happens to land after it — surfacing as
// exactly one randomly-chosen spec file failing per run.
//
// The concrete case this was written for: a spec called `vi.useFakeTimers()`
// in `beforeEach` and never restored them (`vi.restoreAllMocks()` does NOT
// restore timers, and neither does `vi.resetAllMocks()`). Every later spec in
// that worker then ran with a frozen clock, and the first one to `await`
// anything real-timer-driven — a `RouterTestingHarness` navigation, an
// `HttpTestingController` round-trip — died with "Test timed out in 5000ms".
//
// Resetting here rather than only in the offending specs keeps the invariant
// enforced for specs added later. Individual specs are still expected to clean
// up after themselves; this is the backstop, not the primary contract.
import { afterEach, vi } from 'vitest';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// Fail fast on a real network request.
//
// Angular 21 root-provides the entire HttpClient chain: `HttpClient`,
// `HttpHandler` and `HttpBackend` are all `providedIn: 'root'`, and
// `HttpBackend` resolves `useExisting: HttpXhrBackend`. A TestBed that omits
// `provideHttpClientTesting()` therefore does NOT fail with a
// NullInjectorError — it silently gets a live XHR backend and opens real
// sockets to the dev API origin.
//
// That is how `EnvironmentTeardownError: [vitest-worker]: Closing rpc while
// "onUserConsoleLog" was pending` reached CI. Services that load in their
// constructor (`ToolService`, `ModelService`) fired those requests, the
// connections were refused asynchronously, and the resulting `console.error`
// landed after the test that created them had already finished. Vitest
// forwards every console call to the reporter over RPC and rejects any call
// still in flight when the worker tears down; the rejection is unhandled, so
// the run exits non-zero with `Errors 4 errors` and every one of 2,750 tests
// passing. Because worker assignment is timing-dependent (see `isolate: false`
// above), whether a late log lost that race varied run to run — the same
// commit went green on re-run, and the error named whichever spec happened to
// be executing rather than the one that opened the socket.
//
// Unit tests never legitimately reach the network, so refuse the request where
// it is issued rather than letting it fail on a socket some unknown number of
// milliseconds later. A service that catches its own load error still logs, but
// it logs synchronously, inside the test that caused it, and the message names
// the URL — so the next offender is attributable instead of landing on whatever
// spec the worker had moved on to. Specs that exercise a raw-XHR upload path
// replace the global wholesale with `vi.stubGlobal`, which this file's
// `unstubAllGlobals` restores back to the guard.
if (typeof XMLHttpRequest !== 'undefined') {
  const blockedOpen: typeof XMLHttpRequest.prototype.open = function (
    method: string,
    url: string | URL,
  ): void {
    throw new Error(
      `Unit tests must not make real network requests (attempted ${method} ${String(url)}). ` +
        'Add provideHttpClient() and provideHttpClientTesting() to this spec\'s TestBed, ' +
        'or provide a stub for the service that issues the request.',
    );
  };
  XMLHttpRequest.prototype.open = blockedOpen;
}
