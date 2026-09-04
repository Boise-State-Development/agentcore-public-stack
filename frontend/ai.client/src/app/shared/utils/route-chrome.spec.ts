import { describe, it, expect } from 'vitest';
import type { ActivatedRouteSnapshot } from '@angular/router';
import { isMinimalChromeRoute, MINIMAL_CHROME } from './route-chrome';

/** Minimal stand-in for the bits of the snapshot the walk touches. */
function node(
  data: Record<string, unknown>,
  firstChild: unknown = null,
): ActivatedRouteSnapshot {
  return { data, firstChild } as unknown as ActivatedRouteSnapshot;
}

describe('isMinimalChromeRoute', () => {
  it('reads the flag off the deepest route, not the root', () => {
    // The shell reads from the router's ROOT snapshot, but `data` is
    // declared on the route that owns the page. Stopping at the root
    // would never see it — this is the whole reason for the walk.
    const tree = node({}, node({}, node({ chrome: MINIMAL_CHROME })));
    expect(isMinimalChromeRoute(tree)).toBe(true);
  });

  it('is false when no route in the chain asks for it', () => {
    expect(isMinimalChromeRoute(node({}, node({}, node({}))))).toBe(false);
  });

  it('ignores a flag on an ancestor whose leaf does not ask for it', () => {
    // Angular inherits `data` downward, so a leaf that wants full chrome
    // would still report the ancestor's value if we read the wrong node.
    // Reading the leaf is what makes the flag opt-in per page.
    const tree = node({ chrome: MINIMAL_CHROME }, node({ chrome: 'full' }));
    expect(isMinimalChromeRoute(tree)).toBe(false);
  });

  it('handles a single-node tree', () => {
    expect(isMinimalChromeRoute(node({ chrome: MINIMAL_CHROME }))).toBe(true);
  });

  it('is false for a null or undefined root', () => {
    // The shell computes this before the first navigation resolves.
    expect(isMinimalChromeRoute(null)).toBe(false);
    expect(isMinimalChromeRoute(undefined)).toBe(false);
  });

  it('is false for an unrecognized chrome value', () => {
    expect(isMinimalChromeRoute(node({ chrome: 'nope' }))).toBe(false);
  });

  it('tolerates a route with no data at all', () => {
    const bare = { firstChild: null } as unknown as ActivatedRouteSnapshot;
    expect(isMinimalChromeRoute(bare)).toBe(false);
  });
});
