import { TestBed } from '@angular/core/testing';
import { Router, ActivatedRouteSnapshot, RouterStateSnapshot } from '@angular/router';
import { adminGuard } from './admin.guard';
import { SessionService } from './session.service';
import { UserService } from './user.service';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('adminGuard', () => {
  let sessionService: { isAuthenticated: ReturnType<typeof vi.fn> };
  let userService: {
    canAccessAdmin: ReturnType<typeof vi.fn>;
    ensurePermissionsLoaded: ReturnType<typeof vi.fn>;
    getUser: ReturnType<typeof vi.fn>;
  };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let route: ActivatedRouteSnapshot;
  let state: RouterStateSnapshot;

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionService = {
      isAuthenticated: vi.fn(),
    };

    userService = {
      canAccessAdmin: vi.fn(),
      ensurePermissionsLoaded: vi.fn().mockResolvedValue(undefined),
      getUser: vi.fn().mockReturnValue({ roles: [] }),
    };

    router = {
      navigate: vi.fn(),
    };

    route = {} as ActivatedRouteSnapshot;
    state = { url: '/admin/dashboard' } as RouterStateSnapshot;

    vi.spyOn(console, 'warn').mockImplementation(() => {});

    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: sessionService },
        { provide: UserService, useValue: userService },
        { provide: Router, useValue: router },
      ],
    });
  });

  afterEach(() => {
    // Releases the `console.warn` spy installed above — an unrestored spy on a
    // global follows every spec that later shares this worker.
    vi.restoreAllMocks();
    TestBed.resetTestingModule();
  });

  it('should return true when authenticated and user can access admin', async () => {
    sessionService.isAuthenticated.mockReturnValue(true);
    userService.canAccessAdmin.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(true);
    expect(userService.ensurePermissionsLoaded).toHaveBeenCalled();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should redirect to / when authenticated but has no admin access at all', async () => {
    sessionService.isAuthenticated.mockReturnValue(true);
    userService.canAccessAdmin.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(false);
    expect(userService.ensurePermissionsLoaded).toHaveBeenCalled();
    expect(router.navigate).toHaveBeenCalledWith(['/']);
  });

  it('should admit a delegated admin who holds no system_admin role', async () => {
    // The point of the feature: someone with only `admin.skills` gets into the
    // console. Which pages they see inside it is adminScopeGuard's job.
    sessionService.isAuthenticated.mockReturnValue(true);
    userService.canAccessAdmin.mockReturnValue(true);
    userService.getUser.mockReturnValue({ roles: ['faculty'] });

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(true);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should redirect to /auth/login when no BFF session', async () => {
    sessionService.isAuthenticated.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/admin/dashboard' },
    });
    expect(userService.ensurePermissionsLoaded).not.toHaveBeenCalled();
  });
});
