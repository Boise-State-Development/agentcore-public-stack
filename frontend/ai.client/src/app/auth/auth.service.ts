import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';

export interface TokenRefreshRequest {
  refresh_token: string;
}

export interface TokenRefreshResponse {
  access_token: string;
  refresh_token: string;
  id_token?: string;
  token_type: string;
  expires_in: number;
  scope: string;
}

export interface LoginResponse {
  authorization_url: string;
  state: string;
}

@Injectable({
  providedIn: 'root'
})
export class AuthService {
  private http = inject(HttpClient);
  private readonly tokenKey = 'access_token';
  private readonly refreshTokenKey = 'refresh_token';
  private readonly tokenExpiryKey = 'token_expiry';
  private readonly stateKey = 'auth_state';

  /**
   * Get the current access token from localStorage.
   * @returns The access token or null if not found
   */
  getAccessToken(): string | null {
    return localStorage.getItem(this.tokenKey);
  }

  /**
   * Get the refresh token from localStorage.
   * @returns The refresh token or null if not found
   */
  getRefreshToken(): string | null {
    return localStorage.getItem(this.refreshTokenKey);
  }

  /**
   * Check if the current access token is expired or will expire soon.
   * @param bufferSeconds Buffer time in seconds before expiry to consider token expired (default: 60)
   * @returns True if token is expired or will expire soon
   */
  isTokenExpired(bufferSeconds: number = 60): boolean {
    const expiryStr = localStorage.getItem(this.tokenExpiryKey);
    if (!expiryStr) {
      return true; // No expiry info means expired
    }

    const expiryTime = parseInt(expiryStr, 10);
    const currentTime = Date.now();
    const bufferTime = bufferSeconds * 1000;

    return currentTime >= (expiryTime - bufferTime);
  }

  /**
   * Check if user is authenticated (has a valid token).
   * @returns True if user has a token that is not expired
   */
  isAuthenticated(): boolean {
    const token = this.getAccessToken();
    if (!token) {
      return false;
    }
    return !this.isTokenExpired();
  }

  /**
   * Refresh the access token using the refresh token.
   * @returns Promise resolving to the new token response
   */
  async refreshAccessToken(): Promise<TokenRefreshResponse> {
    const refreshToken = this.getRefreshToken();
    if (!refreshToken) {
      throw new Error('No refresh token available');
    }

    try {
      const request: TokenRefreshRequest = {
        refresh_token: refreshToken
      };

      const response = await firstValueFrom(
        this.http.post<TokenRefreshResponse>(
          `${environment.appApiUrl}/auth/refresh`,
          request
        )
      );

      if (!response || !response.access_token) {
        throw new Error('Invalid token refresh response');
      }

      // Store the new tokens
      this.storeTokens(response);

      return response;
    } catch (error) {
      // Clear tokens on refresh failure
      this.clearTokens();
      throw error;
    }
  }

  /**
   * Store tokens in localStorage.
   * @param response Token response containing access_token, refresh_token, and expires_in
   */
  storeTokens(response: { access_token: string; refresh_token?: string; expires_in: number }): void {
    localStorage.setItem(this.tokenKey, response.access_token);

    if (response.refresh_token) {
      localStorage.setItem(this.refreshTokenKey, response.refresh_token);
    }

    // Calculate and store token expiry timestamp
    const expiryTime = Date.now() + response.expires_in * 1000;
    localStorage.setItem(this.tokenExpiryKey, expiryTime.toString());

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
    localStorage.removeItem(this.refreshTokenKey);
    localStorage.removeItem(this.tokenExpiryKey);

    // Dispatch custom event to notify UserService of token removal in same tab
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('token-cleared'));
    }
  }

  /**
   * Get the Authorization header value.
   * @returns Bearer token string or null
   */
  getAuthorizationHeader(): string | null {
    const token = this.getAccessToken();
    return token ? `Bearer ${token}` : null;
  }

  /**
   * Initiates the OIDC login flow by calling the backend login endpoint
   * and redirecting the user to Entra ID for authentication.
   * 
   * Stores the state token in sessionStorage for CSRF protection.
   * The state token will be validated when the authorization code is exchanged.
   * 
   * @param redirectUri Optional redirect URI override
   * @param prompt Optional prompt parameter (defaults to "select_account")
   * @throws Error if login initiation fails
   */
  async login(redirectUri?: string, prompt: string = 'select_account'): Promise<void> {
    try {
      // Build query parameters
      const params = new URLSearchParams();
      if (redirectUri) {
        params.set('redirect_uri', redirectUri);
      }
      params.set('prompt', prompt);

      const queryString = params.toString();
      const url = `${environment.appApiUrl}/auth/login${queryString ? `?${queryString}` : ''}`;

      const response = await firstValueFrom(
        this.http.get<LoginResponse>(url)
      );

      if (!response || !response.authorization_url || !response.state) {
        throw new Error('Invalid login response');
      }

      // Store state token in sessionStorage for CSRF protection
      sessionStorage.setItem(this.stateKey, response.state);

      // Redirect to authorization URL
      window.location.href = response.authorization_url;
    } catch (error) {
      // Clear any stored state on error
      sessionStorage.removeItem(this.stateKey);
      
      if (error instanceof Error) {
        throw error;
      }
      throw new Error('Failed to initiate login');
    }
  }

  /**
   * Get the stored state token from sessionStorage.
   * @returns The state token or null if not found
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
   * Ensures the user is authenticated before making an HTTP request.
   * Attempts to refresh the token if expired, throws an error if authentication fails.
   * 
   * This is a reusable utility for resource loaders and other async operations
   * that require authentication before proceeding.
   * 
   * @throws Error if user is not authenticated and token refresh fails
   * @returns Promise that resolves when user is authenticated
   * 
   * @example
   * ```typescript
   * // In a resource loader
   * readonly myResource = resource({
   *   loader: async () => {
   *     await this.authService.ensureAuthenticated();
   *     return this.http.get('/api/data').toPromise();
   *   }
   * });
   * ```
   */
  async ensureAuthenticated(): Promise<void> {
    // Check if user is authenticated
    if (this.isAuthenticated()) {
      return; // User is authenticated, proceed
    }

    // If not authenticated, try to refresh token if expired
    const token = this.getAccessToken();
    if (token && this.isTokenExpired()) {
      try {
        await this.refreshAccessToken();
        // Verify authentication after refresh
        if (this.isAuthenticated()) {
          return; // Refresh successful, proceed
        }
      } catch (error) {
        // Refresh failed, throw authentication error
        throw new Error('User is not authenticated. Please login again.');
      }
    }

    // No token or refresh failed, throw error
    throw new Error('User is not authenticated. Please login.');
  }
}

