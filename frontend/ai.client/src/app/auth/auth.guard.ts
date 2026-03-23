import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { AuthService } from './auth.service';
import { SystemService } from '../services/system.service';

/**
 * Route guard that protects routes requiring authentication.
 * 
 * Checks if the user is authenticated. If not authenticated:
 * - Attempts to refresh token if expired
 * - Checks system status to redirect to first-boot or login
 * 
 * @returns True if user is authenticated, false otherwise (triggers redirect)
 */
export const authGuard: CanActivateFn = async (route, state) => {
  const authService = inject(AuthService);
  const systemService = inject(SystemService);
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
      // Refresh failed — fall through to redirect logic
    }
  }

  // Check if first-boot is needed before redirecting
  try {
    const firstBootCompleted = await systemService.checkStatus();
    if (!firstBootCompleted) {
      router.navigate(['/auth/first-boot']);
      return false;
    }
  } catch {
    // If status check fails, fall through to login
  }

  // First-boot done (or check failed), redirect to login
  router.navigate(['/auth/login'], { 
    queryParams: { returnUrl: state.url } 
  });
  return false;
};

