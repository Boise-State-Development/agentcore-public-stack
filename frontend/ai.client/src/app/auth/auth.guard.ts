import { inject } from '@angular/core';
import { Router, CanActivateFn } from '@angular/router';
import { SessionService } from './session.service';
import { SystemService } from '../services/system.service';

/**
 * Route guard that protects routes requiring authentication.
 *
 * The BFF session is bootstrapped at app init (`APP_INITIALIZER` chain
 * in `app.config.ts`), so by the time a route resolves
 * `SessionService.bootstrapped()` is already true. Authenticated users
 * pass; everyone else gets routed to first-boot or `/auth/login`, with
 * the deep-link path forwarded as `?returnUrl=` so the BFF callback
 * can land them back where they started.
 */
export const authGuard: CanActivateFn = async (route, state) => {
  const sessionService = inject(SessionService);
  const systemService = inject(SystemService);
  const router = inject(Router);

  if (sessionService.isAuthenticated()) {
    return true;
  }

  try {
    const firstBootCompleted = await systemService.checkStatus();
    if (!firstBootCompleted) {
      router.navigate(['/auth/first-boot']);
      return false;
    }
  } catch {
    // If status check fails, fall through to login.
  }

  router.navigate(['/auth/login'], {
    queryParams: { returnUrl: state.url },
  });
  return false;
};
