import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { SessionService } from './session.service';
import { UserService } from './user.service';

/**
 * Route guard for the `/admin` shell, using backend RBAC resolution.
 *
 * Checks the BFF session for an authenticated user, then resolves permissions
 * from `/users/me/permissions`. Gates on `canAccessAdmin` — `system_admin`
 * **or** any delegated admin scope — because the console is no longer
 * all-or-nothing. Which *pages* inside it are reachable is `adminScopeGuard`'s
 * job; this only decides whether the shell opens at all.
 *
 * Unauthenticated users are bounced through `/auth/login`; authenticated users
 * with no admin access at all land back on the home page.
 */
export const adminGuard: CanActivateFn = async (route, state) => {
  const sessionService = inject(SessionService);
  const userService = inject(UserService);
  const router = inject(Router);

  if (!sessionService.isAuthenticated()) {
    router.navigate(['/auth/login'], {
      queryParams: { returnUrl: state.url }
    });
    return false;
  }

  await userService.ensurePermissionsLoaded();

  if (!userService.canAccessAdmin()) {
    console.warn('User has no admin access:', userService.getUser()?.roles);
    router.navigate(['/']);
    return false;
  }

  return true;
};
