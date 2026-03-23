// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { AuthService, TokenRefreshResponse } from './auth.service';
import { ConfigService } from '../services/config.service';
import { signal } from '@angular/core';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('AuthService', () => {
  let service: AuthService;
  let configService: Partial<ConfigService>;

  // Mock localStorage
  let store: Record<string, string> = {};
  const localStorageMock = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
    removeItem: vi.fn((key: string) => { delete store[key]; }),
    clear: vi.fn(() => { store = {}; }),
  };

  // Mock sessionStorage
  let sessionStore: Record<string, string> = {};
  const sessionStorageMock = {
    getItem: vi.fn((key: string) => sessionStore[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { sessionStore[key] = value; }),
    removeItem: vi.fn((key: string) => { delete sessionStore[key]; }),
    clear: vi.fn(() => { sessionStore = {}; }),
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    store = {};
    sessionStore = {};

    vi.stubGlobal('localStorage', localStorageMock);
    vi.stubGlobal('sessionStorage', sessionStorageMock);

    // Prevent actual redirects
    Object.defineProperty(window, 'location', {
      value: { href: '', origin: 'http://localhost:4200' },
      writable: true,
      configurable: true,
    });

    // Stub window.dispatchEvent to avoid side effects
    vi.spyOn(window, 'dispatchEvent').mockImplementation(() => true);

    configService = {
      appApiUrl: signal('http://localhost:8000') as any,
      cognitoDomainUrl: signal('https://myprefix.auth.us-east-1.amazoncognito.com') as any,
      cognitoAppClientId: signal('test-client-id') as any,
      cognitoRegion: signal('us-east-1') as any,
    };

    TestBed.configureTestingModule({
      providers: [
        AuthService,
        { provide: ConfigService, useValue: configService },
      ],
    });

    service = TestBed.inject(AuthService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.restoreAllMocks();
  });

  // ─── Token Storage ──────────────────────────────────────────────────

  describe('storeTokens', () => {
    it('should store access_token, refresh_token, and expiry in localStorage', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);

      service.storeTokens({
        access_token: 'test-access-token',
        refresh_token: 'test-refresh-token',
        expires_in: 3600,
      });

      expect(localStorageMock.setItem).toHaveBeenCalledWith('access_token', 'test-access-token');
      expect(localStorageMock.setItem).toHaveBeenCalledWith('refresh_token', 'test-refresh-token');
      const expectedExpiry = (now + 3600 * 1000).toString();
      expect(localStorageMock.setItem).toHaveBeenCalledWith('token_expiry', expectedExpiry);
    });

    it('should not store refresh_token if not provided', () => {
      localStorageMock.setItem.mockClear();

      service.storeTokens({
        access_token: 'test-access-token',
        expires_in: 3600,
      });

      const refreshCalls = localStorageMock.setItem.mock.calls.filter(
        (call: string[]) => call[0] === 'refresh_token'
      );
      expect(refreshCalls).toHaveLength(0);
    });
  });

  // ─── Token Retrieval ────────────────────────────────────────────────

  describe('getAccessToken', () => {
    it('should return the stored access token', () => {
      store['access_token'] = 'my-token';
      expect(service.getAccessToken()).toBe('my-token');
    });

    it('should return null when no token is stored', () => {
      expect(service.getAccessToken()).toBeNull();
    });
  });

  describe('getRefreshToken', () => {
    it('should return stored refresh token', () => {
      store['refresh_token'] = 'my-refresh';
      expect(service.getRefreshToken()).toBe('my-refresh');
    });
    it('should return null when no refresh token', () => {
      expect(service.getRefreshToken()).toBeNull();
    });
  });

  // ─── Token Expiry ───────────────────────────────────────────────────

  describe('isTokenExpired', () => {
    it('should return false when token expiry is in the future beyond the buffer', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['token_expiry'] = (now + 120_000).toString();
      expect(service.isTokenExpired()).toBe(false);
    });

    it('should return true when token expiry is within the buffer window', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['token_expiry'] = (now + 30_000).toString();
      expect(service.isTokenExpired()).toBe(true);
    });

    it('should return true when no expiry is stored', () => {
      expect(service.isTokenExpired()).toBe(true);
    });

    it('should return true when token is already expired', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['token_expiry'] = (now - 10_000).toString();
      expect(service.isTokenExpired()).toBe(true);
    });
  });

  // ─── isAuthenticated ─────────────────────────────────────────────────

  describe('isAuthenticated', () => {
    it('should return true when a valid non-expired token exists', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['access_token'] = 'valid-token';
      store['token_expiry'] = (now + 120_000).toString();
      expect(service.isAuthenticated()).toBe(true);
    });

    it('should return false when no token exists', () => {
      expect(service.isAuthenticated()).toBe(false);
    });

    it('should return false when token is expired', () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['access_token'] = 'expired-token';
      store['token_expiry'] = (now - 10_000).toString();
      expect(service.isAuthenticated()).toBe(false);
    });
  });

  // ─── clearTokens ───────────────────────────────────────────────────

  describe('clearTokens', () => {
    it('should remove all auth keys from localStorage', () => {
      store['access_token'] = 'tok';
      store['refresh_token'] = 'ref';
      store['token_expiry'] = '123';
      store['auth_provider_id'] = 'provider1';

      service.clearTokens();

      expect(localStorageMock.removeItem).toHaveBeenCalledWith('access_token');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('refresh_token');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('token_expiry');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('auth_provider_id');
    });

    it('should set currentProviderId signal to null', () => {
      store['auth_provider_id'] = 'provider1';
      service.storeTokens({ access_token: 'x', expires_in: 3600 });
      service.clearTokens();
      expect(service.currentProviderId()).toBeNull();
    });
  });

  // ─── Login (Cognito OAuth 2.0 + PKCE) ──────────────────────────────

  describe('login', () => {
    it('should store state and code_verifier in sessionStorage and redirect to Cognito authorize', async () => {
      await service.login();

      // State and code verifier should be stored in sessionStorage
      expect(sessionStorageMock.setItem).toHaveBeenCalledWith('auth_state', expect.any(String));
      expect(sessionStorageMock.setItem).toHaveBeenCalledWith('auth_code_verifier', expect.any(String));

      // Should redirect to Cognito authorize endpoint
      const href = window.location.href;
      expect(href).toContain('https://myprefix.auth.us-east-1.amazoncognito.com/oauth2/authorize');
      expect(href).toContain('response_type=code');
      expect(href).toContain('client_id=test-client-id');
      expect(href).toContain('code_challenge_method=S256');
      expect(href).toContain('scope=openid+profile+email');
    });

    it('should include identity_provider param when providerId is given', async () => {
      await service.login('Okta');

      const href = window.location.href;
      expect(href).toContain('identity_provider=Okta');
      expect(localStorageMock.setItem).toHaveBeenCalledWith('auth_provider_id', 'Okta');
    });

    it('should not include identity_provider param when no providerId', async () => {
      await service.login();

      const href = window.location.href;
      expect(href).not.toContain('identity_provider');
    });
  });

  // ─── handleCallback ──────────────────────────────────────────────────

  describe('handleCallback', () => {
    it('should exchange code for tokens via Cognito token endpoint', async () => {
      // Set up stored state and code verifier
      sessionStore['auth_state'] = 'test-state';
      sessionStore['auth_code_verifier'] = 'test-verifier';

      const mockResponse: TokenRefreshResponse = {
        access_token: 'new-access-token',
        refresh_token: 'new-refresh-token',
        token_type: 'Bearer',
        expires_in: 3600,
        scope: 'openid profile email',
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      }));

      await service.handleCallback('auth-code-123', 'test-state');

      // Verify fetch was called with correct Cognito token endpoint
      expect(fetch).toHaveBeenCalledWith(
        'https://myprefix.auth.us-east-1.amazoncognito.com/oauth2/token',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        })
      );

      // Verify tokens were stored
      expect(localStorageMock.setItem).toHaveBeenCalledWith('access_token', 'new-access-token');
      expect(localStorageMock.setItem).toHaveBeenCalledWith('refresh_token', 'new-refresh-token');

      // Verify session storage was cleaned up
      expect(sessionStorageMock.removeItem).toHaveBeenCalledWith('auth_state');
      expect(sessionStorageMock.removeItem).toHaveBeenCalledWith('auth_code_verifier');
    });

    it('should throw on state mismatch', async () => {
      sessionStore['auth_state'] = 'stored-state';

      await expect(service.handleCallback('code', 'wrong-state'))
        .rejects.toThrow(/State mismatch/);
    });

    it('should throw when no code verifier is found', async () => {
      sessionStore['auth_state'] = 'test-state';
      // No code verifier stored

      await expect(service.handleCallback('code', 'test-state'))
        .rejects.toThrow(/No code verifier found/);
    });

    it('should throw when token exchange fails', async () => {
      sessionStore['auth_state'] = 'test-state';
      sessionStore['auth_code_verifier'] = 'test-verifier';

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        text: () => Promise.resolve('invalid_grant'),
      }));

      await expect(service.handleCallback('bad-code', 'test-state'))
        .rejects.toThrow(/Token exchange failed/);
    });
  });

  // ─── refreshAccessToken ─────────────────────────────────────────────

  describe('refreshAccessToken', () => {
    it('should refresh tokens via Cognito token endpoint', async () => {
      store['refresh_token'] = 'my-refresh-token';

      const mockResponse: TokenRefreshResponse = {
        access_token: 'refreshed-access-token',
        token_type: 'Bearer',
        expires_in: 3600,
        scope: 'openid profile email',
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      }));

      const result = await service.refreshAccessToken();

      expect(fetch).toHaveBeenCalledWith(
        'https://myprefix.auth.us-east-1.amazoncognito.com/oauth2/token',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        })
      );

      expect(result.access_token).toBe('refreshed-access-token');
      expect(localStorageMock.setItem).toHaveBeenCalledWith('access_token', 'refreshed-access-token');
    });

    it('should throw when no refresh token available', async () => {
      await expect(service.refreshAccessToken()).rejects.toThrow(/No refresh token available/);
    });

    it('should clear tokens on 400/401 from Cognito', async () => {
      store['refresh_token'] = 'bad-refresh';

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        text: () => Promise.resolve('invalid_grant'),
      }));

      await expect(service.refreshAccessToken()).rejects.toThrow(/Token refresh failed/);
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('access_token');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('refresh_token');
    });

    it('should NOT clear tokens on 500 from Cognito', async () => {
      store['refresh_token'] = 'my-refresh';
      localStorageMock.removeItem.mockClear();

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: () => Promise.resolve('Internal Server Error'),
      }));

      await expect(service.refreshAccessToken()).rejects.toThrow(/Token refresh failed/);
      expect(localStorageMock.removeItem).not.toHaveBeenCalledWith('access_token');
      expect(localStorageMock.removeItem).not.toHaveBeenCalledWith('refresh_token');
    });
  });

  // ─── ensureAuthenticated ─────────────────────────────────────────────

  describe('ensureAuthenticated', () => {
    it('should resolve without error when token is valid', async () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['access_token'] = 'valid-token';
      store['token_expiry'] = (now + 120_000).toString();

      await expect(service.ensureAuthenticated()).resolves.toBeUndefined();
    });

    it('should refresh an expired token and resolve on success', async () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['access_token'] = 'expired-token';
      store['refresh_token'] = 'my-refresh-token';
      store['token_expiry'] = (now - 10_000).toString();

      const mockResponse: TokenRefreshResponse = {
        access_token: 'new-access-token',
        refresh_token: 'new-refresh-token',
        token_type: 'Bearer',
        expires_in: 3600,
        scope: 'openid',
      };

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      }));

      await expect(service.ensureAuthenticated()).resolves.toBeUndefined();
    });

    it('should throw when no token exists', async () => {
      await expect(service.ensureAuthenticated()).rejects.toThrow(/not authenticated/i);
    });

    it('should throw when refresh fails', async () => {
      const now = Date.now();
      vi.spyOn(Date, 'now').mockReturnValue(now);
      store['access_token'] = 'expired-token';
      store['refresh_token'] = 'bad-refresh';
      store['token_expiry'] = (now - 10_000).toString();

      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        text: () => Promise.resolve('Unauthorized'),
      }));

      await expect(service.ensureAuthenticated()).rejects.toThrow(/not authenticated/i);
    });
  });

  // ─── Logout ─────────────────────────────────────────────────────────

  describe('logout', () => {
    it('should clear tokens and redirect to Cognito logout endpoint', async () => {
      store['access_token'] = 'token';
      store['refresh_token'] = 'refresh';

      await service.logout();

      expect(localStorageMock.removeItem).toHaveBeenCalledWith('access_token');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('refresh_token');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('token_expiry');
      expect(localStorageMock.removeItem).toHaveBeenCalledWith('auth_provider_id');

      const href = window.location.href;
      expect(href).toContain('https://myprefix.auth.us-east-1.amazoncognito.com/logout');
      expect(href).toContain('client_id=test-client-id');
      expect(href).toContain('logout_uri=');
    });

    it('should redirect to / when Cognito config is not available', async () => {
      // Override config to return empty values
      (configService as any).cognitoDomainUrl = signal('');
      (configService as any).cognitoAppClientId = signal('');

      // Re-create service with empty config
      service = TestBed.inject(AuthService);

      await service.logout();

      expect(window.location.href).toBe('/');
    });
  });

  // ─── Utility Methods ────────────────────────────────────────────────

  describe('getAuthorizationHeader', () => {
    it('should return Bearer header when token exists', () => {
      store['access_token'] = 'my-token';
      expect(service.getAuthorizationHeader()).toBe('Bearer my-token');
    });
    it('should return null when no token', () => {
      expect(service.getAuthorizationHeader()).toBeNull();
    });
  });

  describe('getProviderId', () => {
    it('should return provider ID from localStorage', () => {
      store['auth_provider_id'] = 'provider-1';
      service.storeTokens({ access_token: 'x', expires_in: 3600 });
      expect(service.getProviderId()).toBe('provider-1');
    });
    it('should return null when no provider ID', () => {
      expect(service.getProviderId()).toBeNull();
    });
  });

  describe('event dispatching', () => {
    it('should dispatch token-stored event on storeTokens', () => {
      service.storeTokens({ access_token: 'tok', expires_in: 3600 });
      expect(window.dispatchEvent).toHaveBeenCalled();
    });

    it('should dispatch token-cleared event on clearTokens', () => {
      service.clearTokens();
      expect(window.dispatchEvent).toHaveBeenCalled();
    });
  });
});
