import {
  HttpContextToken,
  HttpInterceptorFn,
  HttpErrorResponse,
} from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { ErrorService } from '../services/error/error.service';
import { SessionService } from './session.service';

/**
 * Set on a request's `HttpContext` to opt out of the global error toast.
 * The caller takes responsibility for surfacing the failure itself — used
 * where a feature renders its own inline, actionable error UI (e.g. the
 * file-source browser handling a 409 with a Connect button).
 */
export const SUPPRESS_ERROR_TOAST = new HttpContextToken<boolean>(() => false);

/**
 * HTTP interceptor that handles errors from non-streaming HTTP requests
 * and displays them to the user via the ErrorService.
 *
 * This interceptor:
 * - Catches HTTP errors from standard (non-SSE) requests
 * - Extracts structured error details from backend responses
 * - Displays user-friendly error messages
 * - Allows errors to propagate for caller-specific handling
 *
 * Note: SSE streaming errors are handled separately in chat-http.service.ts
 */
export const errorInterceptor: HttpInterceptorFn = (req, next) => {
  const errorService = inject(ErrorService);
  const sessionService = inject(SessionService);

  // Skip error handling for SSE streaming endpoints
  // These are handled by fetchEventSource's onerror callback.
  // Only `/chat/stream` (the BFF proxy on app-api) is reachable from
  // the SPA — `/invocations` was removed when the public PKCE client
  // was retired in Phase 7.
  const streamingEndpoints = ['/chat/stream'];
  const isStreamingRequest = streamingEndpoints.some(endpoint =>
    req.url.includes(endpoint)
  );

  if (isStreamingRequest) {
    // Let streaming requests handle their own errors
    return next(req);
  }

  return next(req).pipe(
    catchError((error: unknown) => {
      // Only handle HTTP errors
      if (error instanceof HttpErrorResponse) {
        // Don't show errors for certain endpoints
        const silentEndpoints = ['/health', '/ping'];
        const isSilentEndpoint = silentEndpoints.some(endpoint =>
          req.url.includes(endpoint)
        );

        // 401s mean the BFF session is missing or expired. Route to the
        // SPA login page (idempotent across concurrent 401s) and skip the
        // toast — it just flashes before the redirect lands.
        if (error.status === 401) {
          sessionService.handleUnauthorized();
        } else if (!isSilentEndpoint && !req.context.get(SUPPRESS_ERROR_TOAST)) {
          // Use ErrorService to display the error
          errorService.handleHttpError(error);
        }
      }

      // Always propagate the error so callers can handle it
      return throwError(() => error);
    })
  );
};
