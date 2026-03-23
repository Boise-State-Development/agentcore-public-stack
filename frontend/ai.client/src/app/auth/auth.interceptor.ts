import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { from, catchError, switchMap, throwError } from 'rxjs';
import { AuthService } from './auth.service';
import { ConfigService } from '../services/config.service';

/**
 * HTTP interceptor that automatically adds the Authorization header to outgoing requests
 * and handles token refresh when the access token expires.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const authService = inject(AuthService);
  const configService = inject(ConfigService);

  // Skip adding token for config bootstrap and auth provider listing
  const skipEndpoints = ['/auth/providers', '/config.json'];
  const isSkipEndpoint = skipEndpoints.some(endpoint => req.url.includes(endpoint));

  // If it's a skip endpoint, proceed without modification
  if (isSkipEndpoint) {
    return next(req);
  }

  // Helper function to add token to request
  const addTokenToRequest = (request: typeof req) => {
    const token = authService.getAccessToken();
    if (token) {
      return request.clone({
        setHeaders: {
          Authorization: `Bearer ${token}`
        }
      });
    }
    return request;
  };

  // Get current token
  const token = authService.getAccessToken();
  
  // If no token, proceed without auth header
  if (!token) {
    return next(req);
  }

  // Check if token needs refresh before making the request
  if (authService.isTokenExpired()) {
    // Don't attempt refresh if config isn't loaded yet (empty base URL would
    // produce a relative path that CloudFront resolves to index.html)
    if (!configService.appApiUrl()) {
      return next(req);
    }

    // Token expired, try to refresh it
    return from(authService.refreshAccessToken()).pipe(
      switchMap(() => {
        // Token refreshed, add it to the request
        const clonedReq = addTokenToRequest(req);
        return next(clonedReq);
      }),
      catchError((error) => {
        // Refresh failed, proceed without auth header (will likely fail with 401)
        return next(req);
      })
    );
  }

  // Token is valid, add it to the request
  const clonedReq = addTokenToRequest(req);
  const request$ = next(clonedReq);

  // Handle 401 errors - token might have expired during request
  return request$.pipe(
    catchError((error: HttpErrorResponse) => {
      if (error.status === 401 && !isSkipEndpoint) {
        // Try refreshing token one more time
        return from(authService.refreshAccessToken()).pipe(
          switchMap(() => {
            // Retry the request with new token
            const retryReq = addTokenToRequest(req);
            return next(retryReq);
          }),
          catchError(() => {
            // Refresh failed, clear tokens and return error
            authService.clearTokens();
            return throwError(() => error);
          })
        );
      }
      return throwError(() => error);
    })
  );
};

