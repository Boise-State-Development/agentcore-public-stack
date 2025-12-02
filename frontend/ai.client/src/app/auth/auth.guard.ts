import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { AuthService } from './auth.service';
import { environment } from '../../environments/environment';

/**
 * Route guard that protects routes requiring authentication.
 * 
 * Checks if the user is authenticated. If not authenticated:
 * - Attempts to refresh token if expired
 * - Redirects to /auth/login if refresh fails or no token exists
 * 
 * @returns True if user is authenticated, false otherwise (triggers redirect)
 */
export const authGuard: CanActivateFn = async (route, state) => {
  // If authentication is disabled, allow access to all routes
  if (!environment.enableAuthentication) {
    return true;
  }

  const authService = inject(AuthService);
  const router = inject(Router);

  // Check if user is authenticated
  if (authService.isAuthenticated()) {
    return true;
  }

  // If not authenticated, try to refresh token if expired
  const token = authService.getAccessToken();
  if (token && authService.isTokenExpired()) {
    try {
      await authService.refreshAccessToken();
      // Verify authentication after refresh
      if (authService.isAuthenticated()) {
        return true;
      }
    } catch (error) {
      // Refresh failed, redirect to login
      router.navigate(['/auth/login'], { 
        queryParams: { returnUrl: state.url } 
      });
      return false;
    }
  }

  // No token or refresh failed, redirect to login
  router.navigate(['/auth/login'], { 
    queryParams: { returnUrl: state.url } 
  });
  return false;
};

