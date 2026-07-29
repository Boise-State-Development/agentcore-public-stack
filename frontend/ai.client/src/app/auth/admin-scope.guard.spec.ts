import { TestBed } from '@angular/core/testing';
import { Router, ActivatedRouteSnapshot, RouterStateSnapshot } from '@angular/router';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  adminLandingGuard,
  adminScopeGuard,
  adminLandingCandidates,
} from './admin-scope.guard';
import { UserService } from './user.service';
import { ADMIN_SCOPE_IDS } from '../admin/admin-scope.model';

describe('adminScopeGuard', () => {
  let userService: {
    hasAdminScope: ReturnType<typeof vi.fn>;
    ensurePermissionsLoaded: ReturnType<typeof vi.fn>;
  };
  let router: { createUrlTree: ReturnType<typeof vi.fn> };
  let state: RouterStateSnapshot;

  const routeWith = (data: Record<string, unknown>) =>
    ({ data }) as unknown as ActivatedRouteSnapshot;

  beforeEach(() => {
    TestBed.resetTestingModule();

    userService = {
      hasAdminScope: vi.fn(),
      ensurePermissionsLoaded: vi.fn().mockResolvedValue(undefined),
    };
    router = {
      createUrlTree: vi.fn().mockImplementation((commands: string[]) => ({
        toString: () => commands.join('/'),
        commands,
      })),
    };
    state = { url: '/admin/tools' } as RouterStateSnapshot;

    vi.spyOn(console, 'error').mockImplementation(() => {});

    TestBed.configureTestingModule({
      providers: [
        { provide: UserService, useValue: userService },
        { provide: Router, useValue: router },
      ],
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('admits a user holding the route scope', async () => {
    userService.hasAdminScope.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() =>
      adminScopeGuard(routeWith({ scope: 'admin.tools' }), state)
    );

    expect(result).toBe(true);
    expect(userService.hasAdminScope).toHaveBeenCalledWith('admin.tools');
  });

  it('waits for permissions before deciding', async () => {
    // A guard that reads the signal before the fetch resolves would bounce a
    // legitimate admin on a cold load.
    userService.hasAdminScope.mockReturnValue(true);

    await TestBed.runInInjectionContext(() =>
      adminScopeGuard(routeWith({ scope: 'admin.tools' }), state)
    );

    expect(userService.ensurePermissionsLoaded).toHaveBeenCalled();
  });

  it('redirects to the console landing when the scope is missing', async () => {
    userService.hasAdminScope.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() =>
      adminScopeGuard(routeWith({ scope: 'admin.tools' }), state)
    );

    expect(result).not.toBe(true);
    expect(router.createUrlTree).toHaveBeenCalledWith(['/admin']);
  });

  it('denies a route that declares no scope', async () => {
    // Failing open here would hand every delegated admin a page nobody vetted.
    userService.hasAdminScope.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() =>
      adminScopeGuard(routeWith({}), state)
    );

    expect(result).not.toBe(true);
    expect(userService.hasAdminScope).not.toHaveBeenCalled();
    expect(router.createUrlTree).toHaveBeenCalledWith(['/admin']);
  });
});

describe('adminLandingGuard', () => {
  let userService: {
    hasAdminScope: ReturnType<typeof vi.fn>;
    ensurePermissionsLoaded: ReturnType<typeof vi.fn>;
  };
  let router: { createUrlTree: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    TestBed.resetTestingModule();
    userService = {
      hasAdminScope: vi.fn().mockReturnValue(false),
      ensurePermissionsLoaded: vi.fn().mockResolvedValue(undefined),
    };
    router = { createUrlTree: vi.fn().mockImplementation((c: string[]) => ({ commands: c })) };

    TestBed.configureTestingModule({
      providers: [
        { provide: UserService, useValue: userService },
        { provide: Router, useValue: router },
      ],
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  const runLanding = () =>
    TestBed.runInInjectionContext(() =>
      adminLandingGuard(
        {} as ActivatedRouteSnapshot,
        {} as RouterStateSnapshot
      )
    );

  it('sends a full admin to Cost Analytics', async () => {
    userService.hasAdminScope.mockReturnValue(true);

    await runLanding();

    expect(router.createUrlTree).toHaveBeenCalledWith(['/admin/costs']);
  });

  it('sends a skills-only admin to Skills, not Cost Analytics', async () => {
    // The trap this guard exists for: the old static `redirectTo: 'costs'`
    // dropped every admin on a page a delegated admin cannot open.
    userService.hasAdminScope.mockImplementation(
      (scope: string) => scope === 'admin.skills'
    );

    await runLanding();

    expect(router.createUrlTree).toHaveBeenCalledWith(['/admin/skills']);
  });

  it('sends a marketplace-only admin to the review queue', async () => {
    userService.hasAdminScope.mockImplementation(
      (scope: string) => scope === 'admin.marketplace'
    );

    await runLanding();

    expect(router.createUrlTree).toHaveBeenCalledWith(['/admin/marketplace/review']);
  });

  it('sends a user with no scopes home rather than looping on /admin', async () => {
    userService.hasAdminScope.mockReturnValue(false);

    await runLanding();

    expect(router.createUrlTree).toHaveBeenCalledWith(['/']);
  });

  it('only offers landing targets for known scopes', () => {
    for (const candidate of adminLandingCandidates()) {
      expect(ADMIN_SCOPE_IDS).toContain(candidate.scope);
    }
  });
});
