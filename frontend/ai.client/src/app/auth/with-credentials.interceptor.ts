import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

import { ConfigService } from '../services/config.service';

/**
 * Sets `withCredentials: true` on every request targeting app-api so the
 * browser attaches the `__Host-bff_session` cookie.
 *
 * Production is same-origin via CloudFront `/api/*` and would carry the
 * cookie automatically, but local dev runs the SPA on `localhost:4200`
 * and app-api on `localhost:8000` — cross-origin — so the request must
 * opt in. The flag is harmless on same-origin requests.
 *
 * Scoped to the configured `appApiUrl` (and the `/api/...` same-origin
 * shape) so we don't leak credentials to unrelated third-party hosts a
 * future service might call.
 */
export const withCredentialsInterceptor: HttpInterceptorFn = (req, next) => {
  const appApiUrl = inject(ConfigService).appApiUrl();

  if (!appApiUrl || !req.url.startsWith(appApiUrl)) {
    return next(req);
  }

  return next(req.clone({ withCredentials: true }));
};
