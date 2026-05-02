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
