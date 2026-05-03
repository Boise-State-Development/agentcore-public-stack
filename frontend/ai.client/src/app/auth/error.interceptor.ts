import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { ErrorService } from '../services/error/error.service';

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

        // 401s mean the BFF session is missing or expired. SessionService
        // handles that by routing the user to /auth/login — a toast on top
        // is just noise and tends to flash before the redirect lands.
        const isUnauthorized = error.status === 401;

        if (!isSilentEndpoint && !isUnauthorized) {
          // Use ErrorService to display the error
          errorService.handleHttpError(error);
        }
      }

      // Always propagate the error so callers can handle it
      return throwError(() => error);
    })
  );
};
