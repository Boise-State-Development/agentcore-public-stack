import { TestBed } from '@angular/core/testing';
import { Router, ActivatedRouteSnapshot, RouterStateSnapshot } from '@angular/router';
import { authGuard } from './auth.guard';
import { AuthService } from './auth.service';
import { SystemService } from '../services/system.service';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('authGuard', () => {
  let authService: {
    isAuthenticated: ReturnType<typeof vi.fn>;
    getAccessToken: ReturnType<typeof vi.fn>;
    isTokenExpired: ReturnType<typeof vi.fn>;
    refreshAccessToken: ReturnType<typeof vi.fn>;
  };
  let systemService: { checkStatus: ReturnType<typeof vi.fn> };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let route: ActivatedRouteSnapshot;
  let state: RouterStateSnapshot;

  beforeEach(() => {
    TestBed.resetTestingModule();
    authService = {
      isAuthenticated: vi.fn(),
      getAccessToken: vi.fn(),
      isTokenExpired: vi.fn(),
      refreshAccessToken: vi.fn(),
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
        { provide: AuthService, useValue: authService },
        { provide: Router, useValue: router },
        { provide: SystemService, useValue: systemService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * Validates: Requirements 13.2
   * Authenticated user returns true and allows navigation
   */
  it('should return true when user is authenticated', async () => {
    authService.isAuthenticated.mockReturnValue(true);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(true);
    expect(router.navigate).not.toHaveBeenCalled();
  });

  /**
   * Validates: Requirements 13.3
   * Unauthenticated user with no token redirects to /auth/login with returnUrl
   */
  it('should redirect to /auth/login with returnUrl when not authenticated and no token', async () => {
    authService.isAuthenticated.mockReturnValue(false);
    authService.getAccessToken.mockReturnValue(null);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/dashboard' },
    });
  });

  /**
   * Validates: Requirements 13.4
   * Expired token + successful refresh returns true
   */
  it('should return true when token is expired but refresh succeeds', async () => {
    authService.isAuthenticated
      .mockReturnValueOnce(false)   // initial check
      .mockReturnValueOnce(true);   // after refresh
    authService.getAccessToken.mockReturnValue('expired-token');
    authService.isTokenExpired.mockReturnValue(true);
    authService.refreshAccessToken.mockResolvedValue({});

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(true);
    expect(authService.refreshAccessToken).toHaveBeenCalled();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  /**
   * Validates: Requirements 13.5
   * Expired token + refresh failure redirects to /auth/login
   */
  it('should redirect to /auth/login when token is expired and refresh fails', async () => {
    authService.isAuthenticated.mockReturnValue(false);
    authService.getAccessToken.mockReturnValue('expired-token');
    authService.isTokenExpired.mockReturnValue(true);
    authService.refreshAccessToken.mockRejectedValue(new Error('refresh failed'));

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/dashboard' },
    });
  });

  /**
   * Validates: Requirements 13.3
   * Unauthenticated user with non-expired token but isAuthenticated false still redirects
   */
  it('should redirect when not authenticated and token exists but is not expired', async () => {
    authService.isAuthenticated.mockReturnValue(false);
    authService.getAccessToken.mockReturnValue('some-token');
    authService.isTokenExpired.mockReturnValue(false);

    const result = await TestBed.runInInjectionContext(() => authGuard(route, state));

    expect(result).toBe(false);
    expect(router.navigate).toHaveBeenCalledWith(['/auth/login'], {
      queryParams: { returnUrl: '/dashboard' },
    });
  });
});
