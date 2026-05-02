import { TestBed } from '@angular/core/testing';
import { Router, ActivatedRouteSnapshot, RouterStateSnapshot } from '@angular/router';
import { authGuard } from './auth.guard';
import { SessionService } from './session.service';
import { SystemService } from '../services/system.service';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('authGuard', () => {
  let sessionService: { isAuthenticated: ReturnType<typeof vi.fn> };
  let systemService: { checkStatus: ReturnType<typeof vi.fn> };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let route: ActivatedRouteSnapshot;
  let state: RouterStateSnapshot;

  beforeEach(() => {
    TestBed.resetTestingModule();
    sessionService = {
      isAuthenticated: vi.fn(),
    };

    router = {
      navigate: vi.fn(),
    };

    systemService = {
      checkStatus: vi.fn().mockResolvedValue(true),
    };

    route = {} as ActivatedRouteSnapshot;
    state = { url: '/dashboard' } as RouterStateSnapshot;

    TestBed.configureTestingModule({
      providers: [
        { provide: SessionService, useValue: sessionService },
        { provide: Router, useValue: router },
        { provide: SystemService, useValue: systemService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('should return true when the BFF session is authenticated', async () => {
    sessionService.isAuthenticated.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(true);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('should redirect to /auth/login with returnUrl when no BFF session', async () => {
    sessionService.isAuthenticated.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/dashboard' },
    });
  });

  it('should redirect to first-boot when system status reports incomplete', async () => {
    sessionService.isAuthenticated.mockReturnValue(false);
    systemService.checkStatus.mockResolvedValue(false);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/first-boot']);
  });

  it('should still redirect to /auth/login when system status check throws', async () => {
    sessionService.isAuthenticated.mockReturnValue(false);
    systemService.checkStatus.mockRejectedValue(new Error('boom'));

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/dashboard' },
    });
  });
});
