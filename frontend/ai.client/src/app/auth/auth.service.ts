import { inject, Injectable, computed, signal } from '@angular/core';
import { ConfigService } from '../services/config.service';

export interface TokenRefreshResponse {
  access_token: string;
  refresh_token?: string;
  id_token?: string;
  token_type: string;
  expires_in: number;
  scope?: string;
}

@Injectable({
  providedIn: 'root'
})
export class AuthService {
  private config = inject(ConfigService);
  private readonly tokenKey = 'access_token';
  private readonly idTokenKey = 'id_token';
  private readonly refreshTokenKey = 'refresh_token';
  private readonly tokenExpiryKey = 'token_expiry';
  private readonly stateKey = 'auth_state';
  private readonly codeVerifierKey = 'auth_code_verifier';
  private readonly returnUrlKey = 'auth_return_url';
  private readonly providerIdKey = 'auth_provider_id';

  // Cognito endpoints derived from runtime config
  private readonly cognitoDomain = computed(() => this.config.cognitoDomainUrl());
  private readonly cognitoClientId = computed(() => this.config.cognitoAppClientId());

  private get redirectUri(): string {
    return `${window.location.origin}/auth/callback`;
  }

  private get logoutUri(): string {
    return window.location.origin;
  }

  /**
   * Signal tracking the current authentication provider ID.
   * Used for display purposes and tracking which provider the user authenticated with.
   */
  readonly currentProviderId = signal<string | null>(null);

  constructor() {
    this.updateProviderIdFromStorage();
  }

  /**
   * Get the current access token from localStorage.
   */
  getAccessToken(): string | null {
    return localStorage.getItem(this.tokenKey);
  }

  /**
   * Get the refresh token from localStorage.
   */
  getRefreshToken(): string | null {
    return localStorage.getItem(this.refreshTokenKey);
  }

  /**
   * Check if the current access token is expired or will expire soon.
   * @param bufferSeconds Buffer time in seconds before expiry to consider token expired (default: 60)
   */
  isTokenExpired(bufferSeconds: number = 60): boolean {
    const expiryStr = localStorage.getItem(this.tokenExpiryKey);
    if (!expiryStr) {
      return true;
    }

    const expiryTime = parseInt(expiryStr, 10);
    const currentTime = Date.now();
    const bufferTime = bufferSeconds * 1000;

    return currentTime >= (expiryTime - bufferTime);
  }

  /**
   * Check if user is authenticated (has a valid token).
   */
  isAuthenticated(): boolean {
    const token = this.getAccessToken();
    if (!token) {
      return false;
    }
    return !this.isTokenExpired();
  }

  // ─── PKCE Helpers ───────────────────────────────────────────────────

  /**
   * Generate a cryptographically random code verifier (43-128 chars) for PKCE.
   */
  private generateCodeVerifier(): string {
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    return this.base64UrlEncode(array);
  }

  /**
   * Generate a SHA-256 code challenge from the code verifier for PKCE.
   */
  private async generateCodeChallenge(verifier: string): Promise<string> {
    const encoder = new TextEncoder();
    const data = encoder.encode(verifier);
    const digest = await crypto.subtle.digest('SHA-256', data);
    return this.base64UrlEncode(new Uint8Array(digest));
  }

  /**
   * Generate a random state string for CSRF protection.
   */
  private generateRandomState(): string {
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    return this.base64UrlEncode(array);
  }

  /**
   * Base64url encode a Uint8Array (no padding, URL-safe).
   */
  private base64UrlEncode(buffer: Uint8Array): string {
    let binary = '';
    for (let i = 0; i < buffer.length; i++) {
      binary += String.fromCharCode(buffer[i]);
    }
    return btoa(binary)
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
  }

  // ─── Login ───────────────────────────────────────────────────────────

  /**
   * Initiates the Cognito OAuth 2.0 login flow with PKCE.
   * Redirects the user to the Cognito authorize endpoint.
   *
   * @param providerId Optional Cognito identity provider name for federated login
   */
  async login(providerId?: string): Promise<void> {
    const state = this.generateRandomState();
    const codeVerifier = this.generateCodeVerifier();
    const codeChallenge = await this.generateCodeChallenge(codeVerifier);

    // Store PKCE and state values in sessionStorage
    sessionStorage.setItem(this.stateKey, state);
    sessionStorage.setItem(this.codeVerifierKey, codeVerifier);

    // Store provider ID in localStorage for display purposes
    if (providerId) {
      localStorage.setItem(this.providerIdKey, providerId);
    }

    const params = new URLSearchParams({
      response_type: 'code',
      client_id: this.cognitoClientId(),
      redirect_uri: this.redirectUri,
      scope: 'openid profile email',
      state,
      code_challenge: codeChallenge,
      code_challenge_method: 'S256',
    });

    // If a specific federated provider is selected, add identity_provider param
    if (providerId) {
      params.set('identity_provider', providerId);
    }

    window.location.href = `${this.cognitoDomain()}/oauth2/authorize?${params}`;
  }

  // ─── Callback / Token Exchange ──────────────────────────────────────

  /**
   * Handles the OAuth 2.0 callback by exchanging the authorization code
   * for Cognito tokens directly via the Cognito token endpoint.
   *
   * @param code Authorization code from Cognito
   * @param state State parameter for CSRF verification
   */
  async handleCallback(code: string, state: string): Promise<void> {
    // Verify state matches for CSRF protection
    const storedState = sessionStorage.getItem(this.stateKey);
    if (state !== storedState) {
      this.clearStoredState();
      throw new Error('State mismatch. Security validation failed. Please try logging in again.');
    }

    const codeVerifier = sessionStorage.getItem(this.codeVerifierKey);
    if (!codeVerifier) {
      this.clearStoredState();
      throw new Error('No code verifier found. Please initiate login again.');
    }

    // Exchange code for tokens directly with Cognito
    const response = await fetch(`${this.cognitoDomain()}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: this.cognitoClientId(),
        code,
        redirect_uri: this.redirectUri,
        code_verifier: codeVerifier,
      }),
    });

    if (!response.ok) {
      this.clearStoredState();
      const errorBody = await response.text();
      throw new Error(`Token exchange failed: ${errorBody}`);
    }

    const tokens: TokenRefreshResponse = await response.json();

    if (!tokens || !tokens.access_token) {
      this.clearStoredState();
      throw new Error('Invalid token response from Cognito');
    }

    // Store tokens
    this.storeTokens(tokens);

    // Clean up session storage
    this.clearStoredState();
    sessionStorage.removeItem(this.codeVerifierKey);
  }

  // ─── Token Refresh ───────────────────────────────────────────────────

  /**
   * Refresh the access token using the refresh token via the Cognito token endpoint.
   */
  async refreshAccessToken(): Promise<TokenRefreshResponse> {
    const refreshToken = this.getRefreshToken();
    if (!refreshToken) {
      throw new Error('No refresh token available');
    }

    try {
      const response = await fetch(`${this.cognitoDomain()}/oauth2/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'refresh_token',
          client_id: this.cognitoClientId(),
          refresh_token: refreshToken,
        }),
      });

      if (!response.ok) {
        // On 400/401 from Cognito, the refresh token is invalid — clear tokens
        if (response.status === 400 || response.status === 401) {
          this.clearTokens();
        }
        const errorBody = await response.text();
        throw new Error(`Token refresh failed: ${errorBody}`);
      }

      const tokens: TokenRefreshResponse = await response.json();

      if (!tokens || !tokens.access_token) {
        throw new Error('Invalid token refresh response');
      }

      // Store the new tokens (Cognito refresh_token grant doesn't return a new refresh_token,
      // so we preserve the existing one)
      this.storeTokens(tokens);

      return tokens;
    } catch (error) {
      throw error;
    }
  }

  // ─── Token Storage ──────────────────────────────────────────────────

  /**
   * Store tokens in localStorage.
   */
  storeTokens(response: { access_token: string; refresh_token?: string; id_token?: string; expires_in: number }): void {
    localStorage.setItem(this.tokenKey, response.access_token);

    if (response.id_token) {
      localStorage.setItem(this.idTokenKey, response.id_token);
    }

    if (response.refresh_token) {
      localStorage.setItem(this.refreshTokenKey, response.refresh_token);
    }

    // Calculate and store token expiry timestamp
    const expiryTime = Date.now() + response.expires_in * 1000;
    localStorage.setItem(this.tokenExpiryKey, expiryTime.toString());

    // Update provider ID from localStorage (set during login)
    this.updateProviderIdFromStorage();

    // Dispatch custom event to notify UserService of token change in same tab
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('token-stored', {
        detail: { token: response.access_token }
      }));
    }
  }

  /**
   * Clear all authentication tokens from localStorage.
   */
  clearTokens(): void {
    localStorage.removeItem(this.tokenKey);
    localStorage.removeItem(this.idTokenKey);
    localStorage.removeItem(this.refreshTokenKey);
    localStorage.removeItem(this.tokenExpiryKey);
    localStorage.removeItem(this.providerIdKey);

    this.currentProviderId.set(null);

    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('token-cleared'));
    }
  }

  /**
   * Get the Authorization header value.
   */
  getAuthorizationHeader(): string | null {
    const token = this.getAccessToken();
    return token ? `Bearer ${token}` : null;
  }

  /**
   * Get the stored ID token. Contains user profile claims (email, name, groups).
   */
  getIdToken(): string | null {
    return localStorage.getItem(this.idTokenKey);
  }

  // ─── State / Return URL / Provider ID ────────────────────────────────

  /**
   * Update provider ID from localStorage.
   */
  private updateProviderIdFromStorage(): void {
    const storedProviderId = this.getStoredProviderId();
    this.currentProviderId.set(storedProviderId);
  }

  /**
   * Get the stored state token from sessionStorage.
   */
  getStoredState(): string | null {
    return sessionStorage.getItem(this.stateKey);
  }

  /**
   * Clear the stored state token from sessionStorage.
   */
  clearStoredState(): void {
    sessionStorage.removeItem(this.stateKey);
  }

  /**
   * Get the stored return URL from sessionStorage.
   */
  getStoredReturnUrl(): string | null {
    return sessionStorage.getItem(this.returnUrlKey);
  }

  /**
   * Clear the stored return URL from sessionStorage.
   */
  clearStoredReturnUrl(): void {
    sessionStorage.removeItem(this.returnUrlKey);
  }

  /**
   * Get the stored provider ID from localStorage.
   */
  getStoredProviderId(): string | null {
    return localStorage.getItem(this.providerIdKey);
  }

  /**
   * Get the current provider ID from the signal.
   */
  getProviderId(): string | null {
    return this.currentProviderId();
  }

  // ─── Ensure Authenticated ──────────────────────────────────────────

  /**
   * Ensures the user is authenticated before making an HTTP request.
   * Attempts to refresh the token if expired.
   */
  async ensureAuthenticated(): Promise<void> {
    if (this.isAuthenticated()) {
      return;
    }

    const token = this.getAccessToken();
    if (token && this.isTokenExpired()) {
      try {
        await this.refreshAccessToken();
        if (this.isAuthenticated()) {
          return;
        }
      } catch (error) {
        throw new Error('User is not authenticated. Please login again.');
      }
    }

    throw new Error('User is not authenticated. Please login.');
  }

  // ─── Logout ─────────────────────────────────────────────────────────

  /**
   * Logs the user out by clearing local tokens and redirecting to the
   * Cognito logout endpoint.
   */
  async logout(): Promise<void> {
    // Clear local tokens first
    this.clearTokens();

    const cognitoDomain = this.cognitoDomain();
    const clientId = this.cognitoClientId();

    if (cognitoDomain && clientId) {
      // Redirect to Cognito logout endpoint
      const params = new URLSearchParams({
        client_id: clientId,
        logout_uri: this.logoutUri,
      });
      window.location.href = `${cognitoDomain}/logout?${params}`;
    } else {
      // Fallback: redirect to home if Cognito config not available
      window.location.href = '/';
    }
  }
}
