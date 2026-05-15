import { TestBed } from '@angular/core/testing';
import { Router, ActivatedRouteSnapshot, RouterStateSnapshot } from '@angular/router';
import { adminGuard } from './admin.guard';
import { SessionService } from './session.service';
import { UserService } from './user.service';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('adminGuard', () => {
  let sessionService: { isAuthenticated: ReturnType<typeof vi.fn> };
  let userService: {
    isAdmin: ReturnType<typeof vi.fn>;
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
      isAdmin: vi.fn(),
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
    TestBed.resetTestingModule();
  });

  it('should return true when authenticated and user has system_admin AppRole', async () => {
    sessionService.isAuthenticated.mockReturnValue(true);
    userService.isAdmin.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(true);
    expect(userService.ensurePermissionsLoaded).toHaveBeenCalled();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should redirect to / when authenticated but lacks system_admin AppRole', async () => {
    sessionService.isAuthenticated.mockReturnValue(true);
    userService.isAdmin.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() => adminGuard(route, state));

    expect(result).toBe(false);
    expect(userService.ensurePermissionsLoaded).toHaveBeenCalled();
    expect(router.navigate).toHaveBeenCalledWith(['/']);
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
