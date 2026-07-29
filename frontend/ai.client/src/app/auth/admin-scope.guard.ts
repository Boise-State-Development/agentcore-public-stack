import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { UserService } from './user.service';
import { AdminScopeId } from '../admin/admin-scope.model';

/**
 * Route data contract for `adminScopeGuard`.
 *
 * Declared as a type so a route can be written
 * `data: { scope: 'admin.tools' } satisfies AdminScopeRouteData` and a typo is
 * caught at build time rather than becoming a silently unguarded page.
 */
export interface AdminScopeRouteData {
  scope: AdminScopeId;
}

/**
 * Per-page guard for the admin console.
 *
 * Reads the required scope off the route's `data.scope` and checks it against
 * the user's resolved permissions. `system_admin` passes everything.
 *
 * **This is defence in depth, not the security boundary.** The server enforces
 * the same scope on every admin route (`require_admin_scope`); hiding a page
 * the API would refuse anyway is a UX concern — the point is that a delegated
 * admin never sees a 403 screen for a page they were never meant to reach.
 *
 * A route wired with no `data.scope` is treated as **denied**, not allowed:
 * the likely cause is a page added without wiring its scope, and failing open
 * there would hand every delegated admin a surface nobody vetted.
 */
export const adminScopeGuard: CanActivateFn = async (route, state) => {
  const userService = inject(UserService);
  const router = inject(Router);

  await userService.ensurePermissionsLoaded();

  const scope = route.data?.['scope'] as string | undefined;

  if (!scope) {
    console.error(
      `Admin route "${state.url}" has no data.scope — denying. ` +
        'Add `data: { scope: "admin.<area>" }` to its route definition.'
    );
    return router.createUrlTree(['/admin']);
  }

  if (userService.hasAdminScope(scope)) {
    return true;
  }

  // Back to the console rather than the home page: the user *is* an admin of
  // something, so drop them at the landing route, which resolves to the first
  // area they can actually open.
  return router.createUrlTree(['/admin']);
};

/**
 * Ordered candidates for the `/admin` landing page.
 *
 * Kept in the nav's own order so "first area you can open" matches what the
 * sidebar shows at the top, rather than an arbitrary internal ordering.
 */
const LANDING_CANDIDATES: ReadonlyArray<{ scope: AdminScopeId; path: string }> = [
  { scope: 'admin.costs', path: '/admin/costs' },
  { scope: 'admin.quota', path: '/admin/quota' },
  { scope: 'admin.fine_tuning', path: '/admin/fine-tuning' },
  { scope: 'admin.models', path: '/admin/manage-models' },
  { scope: 'admin.tools', path: '/admin/tools' },
  { scope: 'admin.skills', path: '/admin/skills' },
  { scope: 'admin.connectors', path: '/admin/connectors' },
  { scope: 'admin.marketplace', path: '/admin/marketplace/review' },
  { scope: 'admin.users', path: '/admin/users' },
  { scope: 'admin.roles', path: '/admin/roles' },
  { scope: 'admin.auth_providers', path: '/admin/auth-providers' },
  { scope: 'admin.user_menu_links', path: '/admin/manage-user-menu-links' },
  { scope: 'admin.system_prompts', path: '/admin/system-prompts' },
];

/**
 * Landing redirect for `/admin`.
 *
 * Replaces a static `redirectTo: 'costs'`, which sent every admin to Cost
 * Analytics — fine when admin was all-or-nothing, but a skills-only admin
 * would land on a page they cannot open and bounce straight back out.
 *
 * Always returns a `UrlTree`, so the route it guards is never activated.
 */
export const adminLandingGuard: CanActivateFn = async () => {
  const userService = inject(UserService);
  const router = inject(Router);

  await userService.ensurePermissionsLoaded();

  const target = LANDING_CANDIDATES.find(candidate =>
    userService.hasAdminScope(candidate.scope)
  );

  if (target) {
    return router.createUrlTree([target.path]);
  }

  // No scopes at all — `adminGuard` should already have bounced this user, so
  // reaching here means permissions resolved empty. Send them home rather than
  // looping on /admin.
  return router.createUrlTree(['/']);
};

export function adminLandingCandidates(): ReadonlyArray<{
  scope: AdminScopeId;
  path: string;
}> {
  return LANDING_CANDIDATES;
}
