import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { ErrorService } from '../services/error/error.service';
import { environment } from '../../environments/environment';

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
  // These are handled by fetchEventSource's onerror callback
  const streamingEndpoints = ['/chat/invocations', '/chat/stream'];
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

        if (!isSilentEndpoint) {
          // Use ErrorService to display the error
          errorService.handleHttpError(error);
        }
      }

      // Always propagate the error so callers can handle it
      return throwError(() => error);
    })
  );
};
