/**
 * Front-end model for the headless-grant status surfaced by
 * `apis/app_api/runs/routes.py` (`GET/POST/DELETE /runs/grant`).
 *
 * The grant is the platform's standing permission to act as the user
 * without a live session — required before any of the user's schedules
 * can actually fire. `enabled: false` means no active grant exists.
 */
export interface GrantStatus {
  enabled: boolean;
  grantId?: string | null;
  createdAt?: number | null;
  updatedAt?: number | null;
  expiresAt?: number | null;
  lastUsedAt?: number | null;
}

export interface GrantRevokeResponse {
  revoked: boolean;
}
