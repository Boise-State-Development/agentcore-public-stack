/**
 * The admin console's scope wiring holds together.
 *
 * SPA counterpart to `tests/architecture/test_admin_scope_coverage.py`. Two
 * silent failures this exists to catch:
 *
 *  1. An admin page added without `data.scope` — the guard denies it, so the
 *     page becomes unreachable for *everyone* including full admins, with no
 *     error anywhere.
 *  2. A nav entry whose scope disagrees with its route's scope — the link shows
 *     for someone the guard will bounce, which reads as a broken console.
 */
import { TestBed } from '@angular/core/testing';
import { provideRouter, Routes, Route } from '@angular/router';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import { adminRoutes } from './admin.routes';
import { AdminLayout } from './admin.layout';
import { AdminMarketplaceService } from './marketplace/services/admin-marketplace.service';
import { UserService } from '../auth/user.service';
import { ADMIN_SCOPE_IDS } from './admin-scope.model';
import { adminScopeGuard } from '../auth/admin-scope.guard';

/**
 * Routes that legitimately carry no scope: pure redirects (which resolve to a
 * guarded target) and the landing route (which has its own guard and always
 * redirects).
 */
function isExempt(route: Route): boolean {
  return route.redirectTo !== undefined || route.path === '';
}

function scopedRoutes(routes: Routes): Route[] {
  return routes.filter(r => !isExempt(r));
}

describe('admin route scope wiring', () => {
  it('gives every non-redirect admin route a scope and the scope guard', () => {
    const unguarded: string[] = [];

    for (const route of scopedRoutes(adminRoutes)) {
      const scope = route.data?.['scope'];
      const guarded = route.canActivate?.includes(adminScopeGuard);
      if (!scope || !guarded) {
        unguarded.push(
          `${route.path} (scope: ${scope ?? 'MISSING'}, guard: ${guarded ? 'yes' : 'MISSING'})`
        );
      }
    }

    expect(unguarded).toEqual([]);
  });

  it('only uses scopes that exist in the registry', () => {
    for (const route of scopedRoutes(adminRoutes)) {
      expect(ADMIN_SCOPE_IDS).toContain(route.data?.['scope']);
    }
  });

  it('guards the landing route instead of statically redirecting', () => {
    const landing = adminRoutes.find(r => r.path === '');

    expect(landing).toBeDefined();
    // A static redirectTo would send a delegated admin to a page they cannot
    // open; the guard resolves the first area they can reach.
    expect(landing?.redirectTo).toBeUndefined();
    expect(landing?.canActivate?.length).toBe(1);
  });
});

describe('admin nav scope wiring', () => {
  let userService: { hasAdminScope: ReturnType<typeof vi.fn>; canAccessAdmin: () => boolean };

  function buildLayout(hasScope: (scope: string) => boolean) {
    TestBed.resetTestingModule();
    userService = {
      hasAdminScope: vi.fn().mockImplementation(hasScope),
      canAccessAdmin: () => true,
    };

    TestBed.configureTestingModule({
      providers: [
        // A real router: the layout's template uses routerLink, which needs
        // more than a Router stub once the component actually renders.
        provideRouter([]),
        { provide: UserService, useValue: userService },
        {
          provide: AdminMarketplaceService,
          useValue: {
            pendingCount: signal(0),
            openReportCount: signal(0),
            refreshQueueCounts: vi.fn().mockResolvedValue(undefined),
          },
        },
      ],
    });

    return TestBed.createComponent(AdminLayout).componentInstance;
  }

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('points every nav entry at a route carrying the same scope', () => {
    const layout = buildLayout(() => true);

    const routeScopeByPath = new Map<string, unknown>();
    for (const route of adminRoutes) {
      if (route.path !== undefined && route.data?.['scope']) {
        routeScopeByPath.set(`/admin/${route.path}`, route.data['scope']);
      }
    }

    const mismatches: string[] = [];
    for (const group of layout.navGroups()) {
      for (const item of group.items) {
        const routeScope = routeScopeByPath.get(item.route);
        if (routeScope === undefined) {
          mismatches.push(`${item.route} — nav links to a route with no scope`);
        } else if (routeScope !== item.scope) {
          mismatches.push(
            `${item.route} — nav says ${item.scope}, route says ${routeScope}`
          );
        }
      }
    }

    expect(mismatches).toEqual([]);
  });

  it('shows every group to a full admin', () => {
    const layout = buildLayout(() => true);

    // hasAdminScope short-circuits on system_admin, so a full admin's console
    // must be byte-identical to what it was before scopes existed.
    expect(layout.navGroups().length).toBe(5);
  });

  it('shows a skills-only admin exactly one entry', () => {
    const layout = buildLayout(scope => scope === 'admin.skills');

    const groups = layout.navGroups();
    expect(groups.length).toBe(1);
    expect(groups[0].items.length).toBe(1);
    expect(groups[0].items[0].route).toBe('/admin/skills');
  });

  it('drops a group whose items are all filtered out', () => {
    const layout = buildLayout(scope => scope === 'admin.costs');

    const labels = layout.navGroups().map(g => g.label);
    expect(labels).toEqual(['Usage & Spend']);
  });

  it('skips the marketplace badge fetch without the marketplace scope', () => {
    // Otherwise it is a guaranteed 403 on every admin navigation.
    const layout = buildLayout(scope => scope === 'admin.costs');
    const marketplace = TestBed.inject(AdminMarketplaceService);

    layout.ngOnInit();

    expect(marketplace.refreshQueueCounts).not.toHaveBeenCalled();
  });

  it('still fetches the marketplace badge with the scope', () => {
    const layout = buildLayout(() => true);
    const marketplace = TestBed.inject(AdminMarketplaceService);

    layout.ngOnInit();

    expect(marketplace.refreshQueueCounts).toHaveBeenCalled();
  });
});
