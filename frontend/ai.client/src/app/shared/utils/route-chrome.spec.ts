import { describe, it, expect } from 'vitest';
import type { ActivatedRouteSnapshot } from '@angular/router';
import {
  ADMIN_CHROME,
  isAdminChromeRoute,
  isMinimalChromeRoute,
  MINIMAL_CHROME,
  resolveRouteChrome,
} from './route-chrome';

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

describe('isAdminChromeRoute', () => {
  it('inherits the parent /admin flag onto a child that declares none', () => {
    // This is the whole reason the walk keeps the deepest *declared* value
    // rather than reading the leaf. `data` is declared once on the `/admin`
    // route, and with the default paramsInheritanceStrategy ('emptyOnly')
    // Angular does not copy it onto a child that has its own path and
    // component — which every admin page does. A leaf-only read would see
    // nothing on all ~30 of them.
    const tree = node({}, node({ chrome: ADMIN_CHROME }, node({ scope: 'admin.costs' })));
    expect(isAdminChromeRoute(tree)).toBe(true);
  });

  it('lets a leaf opt back out of an inherited flag', () => {
    const tree = node({ chrome: ADMIN_CHROME }, node({ chrome: 'full' }));
    expect(isAdminChromeRoute(tree)).toBe(false);
  });

  it('does not report admin chrome for a chat route', () => {
    expect(isAdminChromeRoute(node({}, node({})))).toBe(false);
  });

  it('is false for a null root', () => {
    expect(isAdminChromeRoute(null)).toBe(false);
  });

  it('keeps the two modes disjoint', () => {
    // The shell asks both questions of the same snapshot; a tree must never
    // answer yes to both, or the sidenav would render for a route that asked
    // for a stripped shell.
    const admin = node({ chrome: ADMIN_CHROME }, node({}));
    const minimal = node({ chrome: MINIMAL_CHROME });

    expect([isAdminChromeRoute(admin), isMinimalChromeRoute(admin)]).toEqual([true, false]);
    expect([isAdminChromeRoute(minimal), isMinimalChromeRoute(minimal)]).toEqual([false, true]);
  });
});

describe('resolveRouteChrome', () => {
  it('returns null when nothing in the chain declares a chrome', () => {
    expect(resolveRouteChrome(node({}, node({})))).toBeNull();
  });

  it('returns the deepest declared value', () => {
    expect(resolveRouteChrome(node({ chrome: ADMIN_CHROME }, node({ chrome: MINIMAL_CHROME })))).toBe(
      MINIMAL_CHROME,
    );
  });

  it('ignores a non-string declaration', () => {
    expect(resolveRouteChrome(node({ chrome: 1 }))).toBeNull();
  });
});
