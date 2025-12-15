import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { AuthService } from './auth.service';
import { UserService } from './user.service';
import { environment } from '../../environments/environment';

/**
 * Route guard that protects admin routes requiring specific roles.
 *
 * Checks if the user is authenticated and has one of the required admin roles:
 * - Admin
 * - SuperAdmin
 * - DotNetDevelopers
 *
 * If not authenticated, redirects to /auth/login.
 * If authenticated but lacks required role, redirects to home page.
 *
 * @returns True if user is authenticated and has required role, false otherwise
 */
export const adminGuard: CanActivateFn = async (route, state) => {
  // If authentication is disabled, allow access to all routes
  if (!environment.enableAuthentication) {
    return true;
  }

  const authService = inject(AuthService);
  const userService = inject(UserService);
  const router = inject(Router);

  // Check if user is authenticated
  if (!authService.isAuthenticated()) {
    // If not authenticated, try to refresh token if expired
    const token = authService.getAccessToken();
    if (token && authService.isTokenExpired()) {
      try {
        await authService.refreshAccessToken();
        userService.refreshUser();
      } catch (error) {
        // Refresh failed, redirect to login
        router.navigate(['/auth/login'], {
          queryParams: { returnUrl: state.url }
        });
        return false;
      }
    } else {
      // No token or refresh failed, redirect to login
      router.navigate(['/auth/login'], {
        queryParams: { returnUrl: state.url }
      });
      return false;
    }
  }

  // User is authenticated, check for admin roles
  const requiredRoles = ['Admin', 'SuperAdmin', 'DotNetDevelopers'];
  const hasRequiredRole = userService.hasAnyRole(requiredRoles);

  if (!hasRequiredRole) {
    // User doesn't have required role, redirect to home
    console.warn('User lacks required admin role:', userService.getUser()?.roles);
    router.navigate(['/']);
    return false;
  }

  return true;
};
