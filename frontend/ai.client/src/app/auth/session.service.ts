import { computed, Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse, HttpHeaders } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../services/config.service';
import { BffLogoutResponse, BffSessionResponse, BffSessionUser } from './bff-session.model';

/**
 * SessionService — backs the BFF Token-Handler cookie session.
 *
 * Responsibilities:
 *   - Bootstrap by calling `GET {appApiUrl}/auth/session`. The
 *     `__Host-bff_session` cookie travels automatically because the BFF
 *     is same-origin via CloudFront `/api/*`.
 *   - Expose signals for the current user and the CSRF token.
 *   - On 401, redirect the browser to the SPA's `/auth/login` page so the
 *     user picks a provider before we hand off to Cognito Hosted UI.
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

  /**
   * Latch flipped the first time we trigger a 401 redirect, so concurrent
   * 401s from in-flight requests don't queue multiple navigations.
   */
  private redirecting = false;

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
   * Fetch the current session from the BFF. On 401 the browser is sent
   * to the SPA's `/auth/login` page (unless we're already there) so the
   * user can pick a provider before we hand off to Cognito Hosted UI.
   *
   * Fast path: the JS-readable `__Host-bff_csrf` cookie shares its
   * lifetime with the httpOnly session cookie (BFF sets and clears them
   * together). If it's gone, the session is gone too — skip the round-trip
   * and bounce straight to login.
   *
   * Network errors leave the service in a clean unauthenticated state
   * without redirecting — a transient failure shouldn't kick the user
   * out.
   */
  async bootstrap(): Promise<void> {
    if (!this.hasSessionCookie()) {
      this._user.set(null);
      this._csrfToken.set(null);
      if (this.handleUnauthorized()) {
        // Hang so APP_INITIALIZER blocks the SPA from rendering a route
        // before the queued navigation tears the page down.
        await new Promise<never>(() => {});
      }
      this._bootstrapped.set(true);
      return;
    }

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
        if (this.handleUnauthorized()) {
          await new Promise<never>(() => {});
        }
      }
    } finally {
      this._bootstrapped.set(true);
    }
  }

  /**
   * Centralized 401 handler. Clears local session state and navigates the
   * browser to the SPA's `/auth/login` page with a `returnUrl` so the user
   * lands back where they were after re-auth.
   *
   * Idempotent — a concurrent burst of 401s from in-flight requests only
   * queues a single navigation. Skipped (returns false) when we're already
   * on `/auth/login` so the page can render.
   *
   * @returns true when a navigation was queued, false otherwise. Bootstrap
   *   uses the boolean to decide whether to hang APP_INITIALIZER.
   */
  handleUnauthorized(): boolean {
    if (this.redirecting) return false;
    if (window.location.pathname === '/auth/login') return false;
    this.redirecting = true;
    this._user.set(null);
    this._csrfToken.set(null);
    const returnUrl = `${window.location.pathname}${window.location.search}`;
    const params = new URLSearchParams({ returnUrl });
    window.location.href = `/auth/login?${params.toString()}`;
    return true;
  }

  /**
   * Re-probe the session against the BFF. Called by the app shell on tab
   * refocus so a session that expired while the tab was backgrounded
   * surfaces immediately instead of on the next user action.
   *
   * Cookie-presence check first — if the JS-readable CSRF cookie is gone,
   * the session cookie is gone too (they share lifetime), so we redirect
   * without spending a round-trip. Otherwise hits `/auth/session` which has
   * no side effects on the BFF (it neither slides nor refreshes).
   *
   * Network errors are silent — a transient failure mid-tab-focus is not a
   * reason to bounce the user.
   */
  async recheck(): Promise<void> {
    if (this.redirecting) return;
    if (!this._bootstrapped()) return;
    if (!this.hasSessionCookie()) {
      this.handleUnauthorized();
      return;
    }
    const url = `${this.baseUrl()}/auth/session`;
    try {
      const response = await firstValueFrom(
        this.http.get<BffSessionResponse>(url, { withCredentials: true }),
      );
      const { csrf_token, ...user } = response;
      this._user.set(user);
      this._csrfToken.set(csrf_token);
    } catch (error) {
      if (error instanceof HttpErrorResponse && error.status === 401) {
        this.handleUnauthorized();
      }
    }
  }

  /**
   * True when the JS-readable `__Host-bff_csrf` cookie is present. The BFF
   * sets and clears it alongside the httpOnly session cookie with the same
   * `Max-Age`, so absence ⇒ session is also gone. Presence is a weak
   * positive — server-side revocation can leave the cookie behind until
   * its TTL elapses — so callers should still verify with the BFF.
   */
  hasSessionCookie(): boolean {
    if (typeof document === 'undefined') return true;
    return document.cookie
      .split(';')
      .some((entry) => entry.trim().startsWith('__Host-bff_csrf='));
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
   * POST `{appApiUrl}/auth/logout`. The BFF clears its own cookies and
   * returns the Cognito Hosted UI logout URL so we can end the upstream
   * session — without that hop Cognito silently re-issues a code on the
   * next /authorize and the user lands back in without re-auth. We
   * mirror the cookie clear locally and navigate the browser to that
   * URL; the SPA tears down on its own from there. The endpoint is
   * CSRF-exempt server-side, but we send the header anyway so a future
   * tightening doesn't silently break us.
   */
  async logout(): Promise<void> {
    const url = `${this.baseUrl()}/auth/logout`;
    let postLogoutUrl: string | null = null;
    try {
      const response = await firstValueFrom(
        this.http.post<BffLogoutResponse>(url, null, {
          withCredentials: true,
          headers: this.csrfHttpHeaders(),
        }),
      );
      postLogoutUrl = response?.post_logout_url ?? null;
    } finally {
      this._user.set(null);
      this._csrfToken.set(null);
    }
    if (postLogoutUrl) {
      window.location.href = postLogoutUrl;
    }
  }

  private baseUrl(): string {
    // Trim a single trailing slash so `${baseUrl}/auth/session` is
    // well-formed for both `http://localhost:8000` and `/api`.
    const raw = this.config.appApiUrl();
    return raw.endsWith('/') ? raw.slice(0, -1) : raw;
  }
}
