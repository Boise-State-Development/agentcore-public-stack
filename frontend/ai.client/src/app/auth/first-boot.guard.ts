import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { SystemService } from '../services/system.service';

/**
 * Route guard for the first-boot page.
 *
 * Allows access only when first-boot has NOT been completed.
 * If first-boot is already done, redirects to /auth/login.
 */
export const firstBootGuard: CanActivateFn = async () => {
  const systemService = inject(SystemService);
  const router = inject(Router);

  const completed = await systemService.checkStatus();

  if (completed) {
    router.navigate(['/auth/login']);
    return false;
  }

  return true;
};
