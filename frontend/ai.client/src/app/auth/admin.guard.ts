import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { SessionService } from './session.service';
import { UserService } from './user.service';

/**
 * Route guard that protects admin routes using backend RBAC resolution.
 *
 * Checks the BFF session for an authenticated user, then resolves
 * AppRoles from `/users/me/permissions` and gates on `system_admin`.
 * Unauthenticated users are bounced through `/auth/login`; authenticated
 * users without the role land back on the home page.
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

  if (!userService.isAdmin()) {
    console.warn('User lacks system_admin AppRole:', userService.getUser()?.roles);
    router.navigate(['/']);
    return false;
  }

  return true;
};
