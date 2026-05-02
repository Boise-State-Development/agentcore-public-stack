import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

import { SessionService } from './session.service';

/**
 * CSRF interceptor — mirrors the BFF session's CSRF token onto unsafe-method
 * requests so app-api's `CSRFMiddleware` lets them through.
 *
 * Phase 6c wires this in alongside the existing `authInterceptor` (Bearer
 * fallback) and `errorInterceptor`. The two auth paths coexist during the
 * cutover and rollback window:
 *
 *   - Cookie path (post-cutover): browser auto-attaches `__Host-bff_session`
 *     and `__Host-bff_csrf` (same-origin via CloudFront `/api/*`). This
 *     interceptor reads the CSRF value out of `SessionService` (mirrored
 *     into a JS-readable signal during `bootstrap()`) and sets it as the
 *     `X-CSRF-Token` header so server-side double-submit succeeds.
 *
 *   - Bearer fallback (pre-cutover bundle, scripted callers): SessionService
 *     never bootstrapped, `csrfHeaders()` returns `{}`, this interceptor is
 *     a no-op and the request proceeds with whatever auth the caller set.
 *
 * Safe methods (GET/HEAD/OPTIONS) are skipped — `CSRFMiddleware` doesn't
 * enforce on them anyway. `fetchEventSource` lives outside the HttpClient
 * pipeline (chat SSE path), so chat-http.service.ts attaches the header
 * manually using the same `csrfHeaders()` helper.
 */
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export const csrfInterceptor: HttpInterceptorFn = (req, next) => {
  if (SAFE_METHODS.has(req.method)) {
    return next(req);
  }

  const session = inject(SessionService);
  const headers = session.csrfHeaders();

  if (Object.keys(headers).length === 0) {
    return next(req);
  }

  return next(req.clone({ setHeaders: headers }));
};
