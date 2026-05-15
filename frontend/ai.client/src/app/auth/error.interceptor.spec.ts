import { TestBed } from '@angular/core/testing';
import { HttpRequest, HttpResponse, HttpErrorResponse, HttpHandlerFn } from '@angular/common/http';
import { errorInterceptor } from './error.interceptor';
import { ErrorService } from '../services/error/error.service';
import { SessionService } from './session.service';
import { of, throwError } from 'rxjs';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

describe('errorInterceptor', () => {
  let errorService: {
    handleHttpError: ReturnType<typeof vi.fn>;
  };
  let sessionService: {
    handleUnauthorized: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    errorService = {
      handleHttpError: vi.fn(),
    };
    sessionService = {
      handleUnauthorized: vi.fn(),
    };

    TestBed.configureTestingModule({
      providers: [
        { provide: ErrorService, useValue: errorService },
        { provide: SessionService, useValue: sessionService },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * Validates: Requirements 14.8
   * Non-streaming error calls handleHttpError
   */
  it('should call errorService.handleHttpError for non-streaming HTTP errors', async () => {
    const error = new HttpErrorResponse({ status: 500, url: 'http://localhost:8000/api/sessions' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          next: () => resolve(),
          error: () => {
            expect(errorService.handleHttpError).toHaveBeenCalledWith(error);
            resolve();
          },
        });
      });
    });
  });

  it('should call handleHttpError for 400 errors on regular endpoints', async () => {
    const error = new HttpErrorResponse({ status: 400, url: 'http://localhost:8000/api/users' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/api/users');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: () => {
            expect(errorService.handleHttpError).toHaveBeenCalledWith(error);
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Requirements 14.9
   * Streaming endpoint skips error handling
   */
  it('should skip error handling for /chat/stream streaming endpoint', async () => {
    const error = new HttpErrorResponse({ status: 500, url: 'http://localhost:8000/chat/stream' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('POST', 'http://localhost:8000/chat/stream', {});

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: () => {
            expect(errorService.handleHttpError).not.toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Requirements 14.10
   * Silent endpoint skips error display
   */
  it('should skip error display for /health silent endpoint', async () => {
    const error = new HttpErrorResponse({ status: 503, url: 'http://localhost:8000/health' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/health');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: () => {
            expect(errorService.handleHttpError).not.toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });

  it('should skip error display for /ping silent endpoint', async () => {
    const error = new HttpErrorResponse({ status: 503, url: 'http://localhost:8000/ping' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/ping');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: () => {
            expect(errorService.handleHttpError).not.toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Requirements 14.8 (positive case)
   * Error is always propagated even after handling
   */
  it('should propagate the error even after calling handleHttpError', async () => {
    const error = new HttpErrorResponse({ status: 404, url: 'http://localhost:8000/api/sessions/123' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions/123');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: (err: unknown) => {
            expect(errorService.handleHttpError).toHaveBeenCalled();
            expect(err).toBe(error);
            resolve();
          },
        });
      });
    });
  });

  it('should call sessionService.handleUnauthorized on 401 and skip the toast', async () => {
    const error = new HttpErrorResponse({ status: 401, url: 'http://localhost:8000/api/sessions' });
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(throwError(() => error));
    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          error: (err: unknown) => {
            expect(sessionService.handleUnauthorized).toHaveBeenCalledTimes(1);
            expect(errorService.handleHttpError).not.toHaveBeenCalled();
            // Caller still sees the error so any local cleanup runs.
            expect(err).toBe(error);
            resolve();
          },
        });
      });
    });
  });

  /**
   * Validates: Requirements 14.8 (success case)
   * Successful responses pass through without interception
   */
  it('should pass through successful responses without interception', async () => {
    const nextFn: HttpHandlerFn = vi.fn().mockReturnValue(
      of(new HttpResponse({ status: 200, body: { data: 'ok' } }))
    );
    const req = new HttpRequest('GET', 'http://localhost:8000/api/sessions');

    await new Promise<void>((resolve) => {
      TestBed.runInInjectionContext(() => {
        errorInterceptor(req, nextFn).subscribe({
          next: (response) => {
            expect(errorService.handleHttpError).not.toHaveBeenCalled();
            resolve();
          },
        });
      });
    });
  });
});
