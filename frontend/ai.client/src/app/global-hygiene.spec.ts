// @vitest-environment jsdom
//
// Canary for cross-file global pollution.
//
// The unit-test builder runs vitest with `isolate: false`, so every spec file
// assigned to a worker shares one jsdom. A spec that replaces a global and
// never puts it back corrupts whichever unrelated spec happens to run next in
// that worker — which is timing-dependent, so it surfaces as one randomly
// chosen spec file failing per run.
//
// This file asserts the shared jsdom is intact at the moment it runs. It only
// covers whatever executed before it in its worker, so it is a sampling check,
// not a proof: a green run does not mean no spec pollutes, but a red run means
// one definitely does. Treat a failure here as "some other spec leaked", not as
// a bug in this file.
import { describe, expect, it } from 'vitest';

describe('global hygiene', () => {
  it('window.location is the real jsdom Location', () => {
    expect(Object.prototype.toString.call(window.location)).toBe('[object Location]');
    expect(typeof window.location.assign).toBe('function');
  });

  it('document.cookie is the real jsdom accessor, not a shim', () => {
    expect(Object.getOwnPropertyDescriptor(document, 'cookie')).toBeUndefined();
  });

  it('storage globals are the real jsdom Storage', () => {
    expect(Object.prototype.toString.call(sessionStorage)).toBe('[object Storage]');
    expect(Object.prototype.toString.call(localStorage)).toBe('[object Storage]');
  });

  it('document.createElement returns real Elements', () => {
    expect(document.createElement('div')).toBeInstanceOf(HTMLElement);
  });

  it('timers are real', () => {
    // A spec that leaves `vi.useFakeTimers()` installed freezes the clock for
    // every later spec in the worker; anything that awaits a real timer then
    // dies with "Test timed out in 5000ms".
    const before = Date.now();
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        expect(Date.now()).toBeGreaterThanOrEqual(before);
        resolve();
      }, 1);
    });
  });
});
