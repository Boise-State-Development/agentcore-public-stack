/**
 * Delegated admin scopes — the client mirror of
 * `apis/shared/rbac/admin_scopes.py`.
 *
 * A scope names one admin feature area that a system admin can delegate to
 * another role. `system_admin` holds every scope implicitly.
 *
 * The ids are duplicated here as a literal union rather than fetched, because
 * route definitions and nav items need them at build time and a typo in a route
 * `data: { scope }` would otherwise fail open-ended at runtime. The registry
 * *metadata* (labels, groups, descriptions) is still served by
 * `GET /admin/roles/admin-scopes` — this file is only the identifier contract.
 * `admin-scope.model.spec.ts` is the reminder to keep both ends in step.
 */
export const ADMIN_SCOPE_IDS = [
  'admin.costs',
  'admin.quota',
  'admin.fine_tuning',
  'admin.models',
  'admin.tools',
  'admin.skills',
  'admin.connectors',
  'admin.file_sources',
  'admin.export_targets',
  'admin.marketplace',
  'admin.users',
  'admin.system_prompts',
  'admin.user_menu_links',
  'admin.roles',
  'admin.auth_providers',
] as const;

export type AdminScopeId = (typeof ADMIN_SCOPE_IDS)[number];

/**
 * Scopes that can never be granted to a role.
 *
 * Editing a role is how admin power is handed out, and whoever controls IdP
 * claim mapping controls which roles resolve at all — so both stay
 * `system_admin`-only. The server rejects them regardless
 * (`validate_admin_scopes`); this list exists so the picker can render them
 * as unavailable rather than letting an admin submit and get a 400 back.
 */
export const NON_DELEGABLE_SCOPE_IDS: readonly AdminScopeId[] = [
  'admin.roles',
  'admin.auth_providers',
];

/**
 * One registry entry, as returned by `GET /admin/roles/admin-scopes`.
 */
export interface AdminScope {
  id: AdminScopeId;
  label: string;
  /** Matches the admin nav group headings, so the picker reads like the nav. */
  group: string;
  description: string;
  delegable: boolean;
}

export interface AdminScopeListResponse {
  scopes: AdminScope[];
  total: number;
}

export function isNonDelegable(scopeId: string): boolean {
  return (NON_DELEGABLE_SCOPE_IDS as readonly string[]).includes(scopeId);
}
