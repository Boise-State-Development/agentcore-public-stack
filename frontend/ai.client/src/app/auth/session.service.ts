import { computed, Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../services/config.service';
import { BffSessionResponse, BffSessionUser } from './bff-session.model';

/**
 * SessionService — backs the BFF Token-Handler cookie session.
 *
 * Responsibilities:
 *   - Bootstrap by calling `GET {appApiUrl}/auth/session`. The
 *     `__Host-bff_session` cookie travels automatically because the BFF
 *     is same-origin via CloudFront `/api/*`.
 *   - Expose signals for the current user and the CSRF token.
 *   - On 401, redirect the browser to `{appApiUrl}/auth/login` (the BFF
 *     redirects on to Cognito Hosted UI).
 *   - Provide the `X-CSRF-Token` header value for non-GET requests; the
 *     `csrfInterceptor` reads it here and attaches it to outgoing requests.
 *
 * Cookies:
 *   - `__Host-bff_session` (httpOnly) — opaque sealed session id.
 *   - `__Host-bff_csrf` (JS-readable) — double-submit CSRF secret.
 *     The SPA mirrors the value returned in the `csrf_token` response
 *     field via `X-CSRF-Token`; the server compares both sides.
 */
@Injectable({
  providedIn: 'root',
})
export class SessionService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly _user = signal<BffSessionUser | null>(null);
  private readonly _csrfToken = signal<string | null>(null);
  private readonly _bootstrapped = signal(false);

  /** Current BFF session user, or null when no session is active. */
  readonly user = this._user.asReadonly();

  /** CSRF token to mirror in `X-CSRF-Token` on non-GET requests. */
  readonly csrfToken = this._csrfToken.asReadonly();

  /**
   * True once `bootstrap()` has resolved — successfully or not. Lets
   * Phase 6 guards distinguish "session check still pending" from
   * "session check done, no user".
   */
  readonly bootstrapped = this._bootstrapped.asReadonly();

  readonly isAuthenticated = computed(() => this._user() !== null);

  /**
   * Fetch the current session from the BFF. On 401 the browser is
   * redirected to `/auth/login`; the returned promise still resolves
   * (the navigation will tear the page down anyway, but resolving
   * keeps the contract predictable for callers in tests).
   *
   * Network errors leave the service in a clean unauthenticated state
   * without redirecting — a transient failure shouldn't kick the user
   * out.
   */
  async bootstrap(): Promise<void> {
    const url = `${this.baseUrl()}/auth/session`;
    try {
      const response = await firstValueFrom(
        this.http.get<BffSessionResponse>(url, { withCredentials: true }),
      );
      const { csrf_token, ...user } = response;
      this._user.set(user);
      this._csrfToken.set(csrf_token);
    } catch (error) {
      this._user.set(null);
      this._csrfToken.set(null);
      if (error instanceof HttpErrorResponse && error.status === 401) {
        this.redirectToLogin();
      }
    } finally {
      this._bootstrapped.set(true);
    }
  }

  /**
   * Navigate the browser to `{appApiUrl}/auth/login`. The BFF stashes
   * a state cookie and 302s on to Cognito Hosted UI.
   *
   * @param options.returnUrl Optional path to land on after the round-trip.
   *   Same-origin paths only — anything else is dropped server-side and
   *   the user lands on `BFF_POST_LOGIN_REDIRECT_URL`.
   * @param options.providerId Optional federated IdP name (e.g. the
   *   value the SPA's federated-login button knows about). The BFF
   *   forwards it to Cognito as `identity_provider` so the user skips
   *   the Hosted UI chooser and lands on that IdP directly. The BFF
   *   sanitises the value server-side; an unknown provider falls back
   *   to the chooser rather than failing.
   */
  redirectToLogin(options?: { returnUrl?: string; providerId?: string }): void {
    const returnUrl = options?.returnUrl
      ?? `${window.location.pathname}${window.location.search}`;
    const params = new URLSearchParams({ return_to: returnUrl });
    if (options?.providerId) {
      params.set('provider', options.providerId);
    }
    window.location.href = `${this.baseUrl()}/auth/login?${params.toString()}`;
  }

  /**
   * Headers helper for non-GET requests. Returns an empty object when
   * no CSRF token is loaded so callers can spread it unconditionally.
   */
  csrfHeaders(): Record<string, string> {
    const token = this._csrfToken();
    return token ? { 'X-CSRF-Token': token } : {};
  }

  /**
   * `HttpHeaders` form of `csrfHeaders()` for callers that pass an
   * `HttpHeaders` instance to `HttpClient`.
   */
  csrfHttpHeaders(): HttpHeaders {
    return new HttpHeaders(this.csrfHeaders());
  }

  /**
   * POST `{appApiUrl}/auth/logout`. The BFF returns 204 plus cleared
   * cookies; we mirror that by clearing local signals. The endpoint is
   * CSRF-exempt server-side, but we send the header anyway so a future
   * tightening doesn't silently break us.
   */
  async logout(): Promise<void> {
    const url = `${this.baseUrl()}/auth/logout`;
    try {
      await firstValueFrom(
        this.http.post(url, null, {
          withCredentials: true,
          headers: this.csrfHttpHeaders(),
        }),
      );
    } finally {
      this._user.set(null);
      this._csrfToken.set(null);
    }
  }

  private baseUrl(): string {
    // Trim a single trailing slash so `${baseUrl}/auth/session` is
    // well-formed for both `http://localhost:8000` and `/api`.
    const raw = this.config.appApiUrl();
    return raw.endsWith('/') ? raw.slice(0, -1) : raw;
  }
}
