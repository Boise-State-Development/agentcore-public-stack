import { TestBed } from '@angular/core/testing';
import { HttpRequest, HttpResponse, HttpErrorResponse, HttpHandlerFn } from '@angular/common/http';
import { authInterceptor } from './auth.interceptor';
import { AuthService } from './auth.service';
import { ConfigService } from '../services/config.service';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('authInterceptor', () => {
  let authService: {
    getAccessToken: ReturnType<typeof vi.fn>;
    isTokenExpired: ReturnType<typeof vi.fn>;
    refreshAccessToken: ReturnType<typeof vi.fn>;
    clearTokens: ReturnType<typeof vi.fn>;
  };

  let configService: { appApiUrl: ReturnType<typeof signal<string>> };
  let next: HttpHandlerFn;

  beforeEach(() => {
    TestBed.resetTestingModule();
    authService = {
      getAccessToken: vi.fn(),
      isTokenExpired: vi.fn(),
      refreshAccessToken: vi.fn(),
      clearTokens: vi.fn(),
    };

    configService = {
      appApiUrl: signal('http://localhost:8000'),
    };

    next = vi.fn((req: HttpRequest<unknown>) =>
      of(new HttpResponse({ status: 200, body: {} }))
    );

    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: authService },
        { provide: ConfigService, useValue: configService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * Validates: Requirements 14.2
   * Attaches Bearer token to non-auth endpoint requests
   */
  it('should attach Bearer token to outgoing requests', () => {
    authService.getAccessToken.mockReturnValue('my-access-token');
    authService.isTokenExpired.mockReturnValue(false);

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.get('Authorization')).toBe('Bearer my-access-token');
  });

  /**
   * Validates: Requirements 14.3
   * Skips skip endpoints — passes request through without modification
   */
  it('should skip adding token for /auth/providers endpoint', () => {
    authService.getAccessToken.mockReturnValue('my-token');

    const req = new HttpRequest('GET', 'http://localhost:8000/auth/providers');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.has('Authorization')).toBe(false);
  });

  it('should skip adding token for /config.json endpoint', () => {
    authService.getAccessToken.mockReturnValue('my-token');

    const req = new HttpRequest('GET', '/config.json');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.has('Authorization')).toBe(false);
  });

  it('should add token for regular API endpoints', () => {
    authService.getAccessToken.mockReturnValue('my-token');
    authService.isTokenExpired.mockReturnValue(false);

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.get('Authorization')).toBe('Bearer my-token');
  });

  /**
   * Validates: Requirements 14.4
   * No token passes request through without Authorization header
   */
  it('should pass request through without Authorization header when no token exists', () => {
    authService.getAccessToken.mockReturnValue(null);

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.has('Authorization')).toBe(false);
  });

  /**
   * Validates: Requirements 14.5
   * Refreshes expired token before sending request
   */
  it('should refresh expired token before sending request', async () => {
    authService.getAccessToken
      .mockReturnValueOnce('expired-token')  // initial check
      .mockReturnValue('new-token');          // after refresh
    authService.isTokenExpired.mockReturnValue(true);
    authService.refreshAccessToken.mockResolvedValue({});

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve, reject) => {
      TestBed.runInInjectionContext(() => {
        authInterceptor(req, next).subscribe({
          next: () => resolve(),
          error: reject,
        });
      });
    });

    expect(authService.refreshAccessToken).toHaveBeenCalled();
    // After refresh, the request should be sent with the new token
    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.get('Authorization')).toBe('Bearer new-token');
  });

  /**
   * Validates: Requirements 14.6
   * Retries on 401 response with refreshed token
   */
  it('should retry request with refreshed token on 401 error', async () => {
    authService.getAccessToken
      .mockReturnValueOnce('old-token')   // initial request
      .mockReturnValue('refreshed-token'); // after refresh for retry
    authService.isTokenExpired.mockReturnValue(false);
    authService.refreshAccessToken.mockResolvedValue({});

    const error401 = new HttpErrorResponse({ status: 401, url: 'http://localhost:8000/api/sessions' });

    // First call returns 401, second call succeeds
    const nextFn = vi.fn()
      .mockReturnValueOnce(throwError(() => error401))
      .mockReturnValueOnce(of(new HttpResponse({ status: 200, body: {} })));

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve, reject) => {
      TestBed.runInInjectionContext(() => {
        authInterceptor(req, nextFn as HttpHandlerFn).subscribe({
          next: () => resolve(),
          error: reject,
        });
      });
    });

    expect(authService.refreshAccessToken).toHaveBeenCalled();
    // Should have been called twice: initial + retry
    expect(nextFn).toHaveBeenCalledTimes(2);
    const retryReq = nextFn.mock.calls[1][0] as HttpRequest<unknown>;
    expect(retryReq.headers.get('Authorization')).toBe('Bearer refreshed-token');
  });

  /**
   * Validates: Requirements 14.7
   * 401 retry fail clears tokens and propagates error
   */
  it('should clear tokens and propagate error when 401 retry refresh fails', async () => {
    authService.getAccessToken.mockReturnValue('old-token');
    authService.isTokenExpired.mockReturnValue(false);
    authService.refreshAccessToken.mockRejectedValue(new Error('refresh failed'));

    const error401 = new HttpErrorResponse({ status: 401, url: 'http://localhost:8000/api/sessions' });

    const nextFn = vi.fn().mockReturnValue(throwError(() => error401));

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        authInterceptor(req, nextFn as HttpHandlerFn).subscribe({
          next: () => resolve(),
          error: (err: HttpErrorResponse) => {
            expect(err.status).toBe(401);
            expect(authService.clearTokens).toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Requirements 14.6 (edge case)
   * Non-401 errors are propagated without retry
   */
  it('should propagate non-401 errors without retry', async () => {
    authService.getAccessToken.mockReturnValue('valid-token');
    authService.isTokenExpired.mockReturnValue(false);

    const error500 = new HttpErrorResponse({ status: 500, url: 'http://localhost:8000/api/sessions' });

    const nextFn = vi.fn().mockReturnValue(throwError(() => error500));

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        authInterceptor(req, nextFn as HttpHandlerFn).subscribe({
          next: () => resolve(),
          error: (err: HttpErrorResponse) => {
            expect(err.status).toBe(500);
            expect(authService.refreshAccessToken).not.toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Issue #24 fix
   * Skips token refresh when config is not loaded (appApiUrl is empty)
   */
  it('should skip token refresh when appApiUrl is empty (config not loaded)', () => {
    configService.appApiUrl.set('');
    authService.getAccessToken.mockReturnValue('expired-token');
    authService.isTokenExpired.mockReturnValue(true);

    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    expect(authService.refreshAccessToken).not.toHaveBeenCalled();
    // Request should pass through without auth header
    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.has('Authorization')).toBe(false);
  });

  /**
   * Validates: Issue #24 fix
   * Skips auth processing for /config.json requests
   */
  it('should skip auth processing for /config.json requests', () => {
    authService.getAccessToken.mockReturnValue('my-token');
    authService.isTokenExpired.mockReturnValue(true);

    const req = new HttpRequest('GET', '/config.json');

    TestBed.runInInjectionContext(() => {
      authInterceptor(req, next);
    });

    expect(authService.refreshAccessToken).not.toHaveBeenCalled();
    const passedReq = (next as ReturnType<typeof vi.fn>).mock.calls[0][0] as HttpRequest<unknown>;
    expect(passedReq.headers.has('Authorization')).toBe(false);
  });
});
