// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionService } from './session.service';
import { ConfigService } from '../services/config.service';

describe('SessionService', () => {
  let service: SessionService;
  let httpMock: HttpTestingController;
  let configService: Partial<ConfigService>;

  const sessionResponse = {
    user_id: 'u-123',
    email: 'phil@example.com',
    name: 'Phil Merrell',
    roles: ['user'],
    picture: null,
    csrf_token: 'csrf-secret-abc',
  };

  // Helpers — bootstrap takes the network path only when the JS-readable
  // CSRF cookie is present; otherwise the fast-path bounces straight to
  // login. Tests that exercise the network path set the cookie first.
  //
  // jsdom enforces `__Host-` cookie prefix rules (Secure required, no
  // http://localhost), so a real `document.cookie` write is silently
  // rejected. Install a minimal one-cookie shim per-test.
  let cookieStore = '';
  const installCookieShim = () => {
    cookieStore = '';
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => cookieStore,
      set: (input: string) => {
        const [pair, ...attrs] = input.split(';');
        const expired = attrs.some((a) => /expires=Thu, 01 Jan 1970/i.test(a));
        cookieStore = expired ? '' : pair.trim();
      },
    });
  };
  const setCsrfCookie = (value = 'test-csrf-token') => {
    document.cookie = `__Host-bff_csrf=${value}; path=/`;
  };
  const clearCsrfCookie = () => {
    document.cookie =
      '__Host-bff_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    installCookieShim();

    Object.defineProperty(window, 'location', {
      value: {
        href: '',
        origin: 'http://localhost:4200',
        pathname: '/',
        search: '',
      },
      writable: true,
      configurable: true,
    });

    configService = {
      appApiUrl: signal('http://localhost:8000') as any,
    };

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        SessionService,
        { provide: ConfigService, useValue: configService },
      ],
    });

    service = TestBed.inject(SessionService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    clearCsrfCookie();
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  describe('initial state', () => {
    it('starts unauthenticated with no user, no csrf, not bootstrapped', () => {
      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(service.bootstrapped()).toBe(false);
      expect(service.isAuthenticated()).toBe(false);
    });
  });

  describe('bootstrap', () => {
    it('populates user and csrfToken on a successful 200', async () => {
      setCsrfCookie();
      const promise = service.bootstrap();

      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      expect(req.request.method).toBe('GET');
      expect(req.request.withCredentials).toBe(true);
      req.flush(sessionResponse);

      await promise;

      expect(service.bootstrapped()).toBe(true);
      expect(service.csrfToken()).toBe('csrf-secret-abc');
      expect(service.isAuthenticated()).toBe(true);

      const user = service.user();
      expect(user).toEqual({
        user_id: 'u-123',
        email: 'phil@example.com',
        name: 'Phil Merrell',
        roles: ['user'],
        picture: null,
      });
      // The CSRF token must not leak into the user signal.
      expect(user as any).not.toHaveProperty('csrf_token');
    });

    it('redirects to the SPA /auth/login page on 401 and clears state', async () => {
      setCsrfCookie();
      window.location.pathname = '/admin/users';
      window.location.search = '?tab=roles';

      // bootstrap() intentionally never resolves on 401 (when navigation is
      // queued) — it hangs to keep APP_INITIALIZER blocking until the queued
      // window.location.href fires. We don't await the promise; we flush
      // microtasks and inspect state.
      void service.bootstrap();

      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.flush('unauthorized', { status: 401, statusText: 'Unauthorized' });

      await new Promise(resolve => setTimeout(resolve, 0));

      expect(service.bootstrapped()).toBe(false);
      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(service.isAuthenticated()).toBe(false);

      const expectedReturn = encodeURIComponent('/admin/users?tab=roles');
      expect(window.location.href).toBe(
        `/auth/login?returnUrl=${expectedReturn}`,
      );
    });

    it('does not redirect when the 401 lands on /auth/login itself', async () => {
      setCsrfCookie();
      window.location.pathname = '/auth/login';
      window.location.search = '';

      const promise = service.bootstrap();

      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.flush('unauthorized', { status: 401, statusText: 'Unauthorized' });

      await promise;

      expect(service.bootstrapped()).toBe(true);
      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(window.location.href).toBe('');
    });

    it('does not redirect on a non-401 transport error', async () => {
      setCsrfCookie();
      const promise = service.bootstrap();

      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.error(new ProgressEvent('network'), { status: 0, statusText: '' });

      await promise;

      expect(service.bootstrapped()).toBe(true);
      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(window.location.href).toBe('');
    });

    it('uses same-origin path when appApiUrl is configured as /api', async () => {
      setCsrfCookie();
      (configService.appApiUrl as any).set('/api');

      const promise = service.bootstrap();

      const req = httpMock.expectOne('/api/auth/session');
      req.flush(sessionResponse);

      await promise;

      expect(service.isAuthenticated()).toBe(true);
    });

    it('strips a trailing slash from appApiUrl', async () => {
      setCsrfCookie();
      (configService.appApiUrl as any).set('http://localhost:8000/');

      const promise = service.bootstrap();

      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.flush(sessionResponse);

      await promise;

      expect(service.isAuthenticated()).toBe(true);
    });

    it('skips the /auth/session round-trip and redirects when no CSRF cookie is present', async () => {
      window.location.pathname = '/files';
      window.location.search = '';
      // Cookie absent (cleared in beforeEach). The fast-path should
      // detect this and bounce without making any HTTP request.
      void service.bootstrap();

      await new Promise(resolve => setTimeout(resolve, 0));

      httpMock.expectNone('http://localhost:8000/auth/session');
      expect(service.bootstrapped()).toBe(false);
      expect(window.location.href).toBe(
        `/auth/login?returnUrl=${encodeURIComponent('/files')}`,
      );
    });

    it('still resolves on /auth/login when the CSRF cookie is absent', async () => {
      window.location.pathname = '/auth/login';
      window.location.search = '';
      // No cookie — fast-path triggers, but handleUnauthorized returns
      // false on /auth/login so bootstrap completes without hanging.
      await service.bootstrap();

      httpMock.expectNone('http://localhost:8000/auth/session');
      expect(service.bootstrapped()).toBe(true);
      expect(window.location.href).toBe('');
    });
  });

  describe('handleUnauthorized', () => {
    it('redirects to /auth/login with the current path as returnUrl', () => {
      window.location.pathname = '/manage-sessions';
      window.location.search = '?id=abc';

      const navigated = service.handleUnauthorized();

      expect(navigated).toBe(true);
      expect(window.location.href).toBe(
        `/auth/login?returnUrl=${encodeURIComponent('/manage-sessions?id=abc')}`,
      );
      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
    });

    it('is a no-op when already on /auth/login', () => {
      window.location.pathname = '/auth/login';

      const navigated = service.handleUnauthorized();

      expect(navigated).toBe(false);
      expect(window.location.href).toBe('');
    });

    it('dedupes concurrent calls — only the first navigates', () => {
      window.location.pathname = '/files';

      expect(service.handleUnauthorized()).toBe(true);

      // Mid-burst 401s shouldn't queue more navigations.
      window.location.href = ''; // simulate that nothing has actually navigated yet
      expect(service.handleUnauthorized()).toBe(false);
      expect(window.location.href).toBe('');
    });
  });

  describe('hasSessionCookie', () => {
    it('returns false when the cookie is absent', () => {
      expect(service.hasSessionCookie()).toBe(false);
    });

    it('returns true when the cookie is present', () => {
      setCsrfCookie();
      expect(service.hasSessionCookie()).toBe(true);
    });
  });

  describe('recheck', () => {
    const bootstrapAuthenticated = async () => {
      setCsrfCookie();
      const promise = service.bootstrap();
      httpMock.expectOne('http://localhost:8000/auth/session').flush(sessionResponse);
      await promise;
    };

    it('is a no-op before bootstrap has resolved', async () => {
      setCsrfCookie();
      await service.recheck();
      httpMock.expectNone('http://localhost:8000/auth/session');
    });

    it('refreshes session state on a successful 200', async () => {
      await bootstrapAuthenticated();
      window.location.pathname = '/files';

      const promise = service.recheck();
      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.flush({ ...sessionResponse, csrf_token: 'rotated-csrf' });
      await promise;

      expect(service.csrfToken()).toBe('rotated-csrf');
      expect(service.isAuthenticated()).toBe(true);
      expect(window.location.href).toBe('');
    });

    it('redirects when the BFF returns 401', async () => {
      await bootstrapAuthenticated();
      window.location.pathname = '/files';

      const promise = service.recheck();
      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.flush('unauthorized', { status: 401, statusText: 'Unauthorized' });
      await promise;

      expect(window.location.href).toBe(
        `/auth/login?returnUrl=${encodeURIComponent('/files')}`,
      );
    });

    it('redirects without a network call when the CSRF cookie is gone', async () => {
      await bootstrapAuthenticated();
      window.location.pathname = '/files';
      clearCsrfCookie();

      await service.recheck();

      httpMock.expectNone('http://localhost:8000/auth/session');
      expect(window.location.href).toBe(
        `/auth/login?returnUrl=${encodeURIComponent('/files')}`,
      );
    });

    it('stays silent on a transient network error', async () => {
      await bootstrapAuthenticated();
      window.location.pathname = '/files';

      const promise = service.recheck();
      const req = httpMock.expectOne('http://localhost:8000/auth/session');
      req.error(new ProgressEvent('network'), { status: 0, statusText: '' });
      await promise;

      expect(window.location.href).toBe('');
      expect(service.isAuthenticated()).toBe(true);
    });
  });

  describe('csrfHeaders', () => {
    it('returns an empty object when no token is loaded', () => {
      expect(service.csrfHeaders()).toEqual({});
    });

    it('returns X-CSRF-Token after a successful bootstrap', async () => {
      setCsrfCookie();
      const promise = service.bootstrap();
      httpMock.expectOne('http://localhost:8000/auth/session').flush(sessionResponse);
      await promise;

      expect(service.csrfHeaders()).toEqual({ 'X-CSRF-Token': 'csrf-secret-abc' });
      expect(service.csrfHttpHeaders().get('X-CSRF-Token')).toBe('csrf-secret-abc');
    });
  });

  describe('redirectToLogin', () => {
    it('uses the current location as the return target by default', () => {
      window.location.pathname = '/files';
      window.location.search = '';

      service.redirectToLogin();

      expect(window.location.href).toBe(
        'http://localhost:8000/auth/login?return_to=%2Ffiles',
      );
    });

    it('respects an explicit returnUrl', () => {
      service.redirectToLogin({ returnUrl: '/somewhere/else' });

      expect(window.location.href).toBe(
        'http://localhost:8000/auth/login?return_to=%2Fsomewhere%2Felse',
      );
    });

    it('forwards providerId as the `provider` query param', () => {
      window.location.pathname = '/';
      window.location.search = '';

      service.redirectToLogin({ providerId: 'GoogleSSO' });

      // The BFF reads `provider` and forwards to Cognito as
      // `identity_provider`, which short-circuits the Hosted UI chooser.
      expect(window.location.href).toBe(
        'http://localhost:8000/auth/login?return_to=%2F&provider=GoogleSSO',
      );
    });

    it('skips the provider param when providerId is empty', () => {
      window.location.pathname = '/';
      window.location.search = '';

      service.redirectToLogin({ providerId: '' });

      expect(window.location.href).toBe(
        'http://localhost:8000/auth/login?return_to=%2F',
      );
    });
  });

  describe('logout', () => {
    it('POSTs /auth/logout, clears local state, and navigates to the Cognito logout URL', async () => {
      setCsrfCookie();
      const bootPromise = service.bootstrap();
      httpMock.expectOne('http://localhost:8000/auth/session').flush(sessionResponse);
      await bootPromise;

      const logoutPromise = service.logout();
      const req = httpMock.expectOne('http://localhost:8000/auth/logout');
      expect(req.request.method).toBe('POST');
      expect(req.request.withCredentials).toBe(true);
      expect(req.request.headers.get('X-CSRF-Token')).toBe('csrf-secret-abc');
      req.flush({
        post_logout_url:
          'https://example.auth.us-east-1.amazoncognito.com/logout?client_id=cid&logout_uri=http%3A%2F%2Flocalhost%3A4200',
      });

      await logoutPromise;

      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(service.isAuthenticated()).toBe(false);
      expect(window.location.href).toBe(
        'https://example.auth.us-east-1.amazoncognito.com/logout?client_id=cid&logout_uri=http%3A%2F%2Flocalhost%3A4200',
      );
    });

    it('does not navigate when post_logout_url is null', async () => {
      setCsrfCookie();
      const bootPromise = service.bootstrap();
      httpMock.expectOne('http://localhost:8000/auth/session').flush(sessionResponse);
      await bootPromise;

      const logoutPromise = service.logout();
      const req = httpMock.expectOne('http://localhost:8000/auth/logout');
      req.flush({ post_logout_url: null });

      await logoutPromise;

      expect(service.user()).toBeNull();
      expect(window.location.href).toBe('');
    });

    it('clears local state even when /auth/logout fails', async () => {
      setCsrfCookie();
      const bootPromise = service.bootstrap();
      httpMock.expectOne('http://localhost:8000/auth/session').flush(sessionResponse);
      await bootPromise;

      const logoutPromise = service.logout();
      const req = httpMock.expectOne('http://localhost:8000/auth/logout');
      req.flush('boom', { status: 500, statusText: 'Server Error' });

      await expect(logoutPromise).rejects.toBeDefined();

      expect(service.user()).toBeNull();
      expect(service.csrfToken()).toBeNull();
      expect(window.location.href).toBe('');
    });
  });
});
