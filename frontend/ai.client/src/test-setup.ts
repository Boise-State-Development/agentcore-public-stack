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
