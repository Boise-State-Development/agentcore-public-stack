import { HttpInterceptorFn, HttpErrorResponse } from '@angular/common/http';
import { inject } from '@angular/core';
import { from, catchError, switchMap, throwError } from 'rxjs';
import { AuthService } from './auth.service';
import { environment } from '../../environments/environment';

/**
 * HTTP interceptor that automatically adds the Authorization header to outgoing requests
 * and handles token refresh when the access token expires.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  // If authentication is disabled, skip all token logic
  if (!environment.enableAuthentication) {
    return next(req);
  }

  const authService = inject(AuthService);

  // Skip adding token for auth endpoints (login, token exchange, refresh)
  const authEndpoints = ['/auth/login', '/auth/token', '/auth/refresh'];
  const isAuthEndpoint = authEndpoints.some(endpoint => req.url.includes(endpoint));

  // If it's an auth endpoint, proceed without modification
  if (isAuthEndpoint) {
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
      if (error.status === 401 && !isAuthEndpoint) {
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

