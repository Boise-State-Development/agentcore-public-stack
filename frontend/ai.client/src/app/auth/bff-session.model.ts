/**
 * BFF session user — payload returned by `GET /auth/session` on the
 * Token-Handler BFF (app-api). Distinct from the Bearer-flow `User`
 * model in `user.model.ts` because the BFF returns a single `name`
 * field rather than split first/last/full names. Phase 6 decides how
 * to normalize between the two.
 */
export interface BffSessionUser {
  user_id: string;
  email: string;
  name: string | null;
  roles: string[];
  picture: string | null;
}

/**
 * Raw `GET /auth/session` response shape — the BFF returns the user
 * payload alongside the CSRF token the SPA must mirror in the
 * `X-CSRF-Token` header on non-GET requests.
 */
export interface BffSessionResponse extends BffSessionUser {
  csrf_token: string;
}

/**
 * `POST /auth/logout` response. The BFF clears its own cookies inline
 * but Cognito's Hosted UI session cookie sits on the Cognito domain —
 * if we don't bounce the browser through `${cognitoDomain}/logout`,
 * Cognito silently re-issues a code on the next /authorize and the
 * user is back in without a credential prompt. The SPA navigates to
 * `post_logout_url` to end that upstream session. Null when Cognito
 * isn't configured (local dev with the BFF in degraded mode).
 */
export interface BffLogoutResponse {
  post_logout_url: string | null;
  /**
   * True when the user's headless grant holds this session's refresh token,
   * so the BFF signed the session out but kept the token alive for scheduled
   * runs. The sign-in page then tells the user their scheduled runs keep going.
   */
  scheduled_runs_continue?: boolean;
}

/** sessionStorage key that carries `scheduled_runs_continue` across the Cognito logout hop. */
export const SCHEDULED_RUNS_CONTINUE_KEY = 'signed-out-scheduled-runs-continue';
