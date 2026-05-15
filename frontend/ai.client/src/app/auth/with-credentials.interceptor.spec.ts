import { TestBed } from '@angular/core/testing';
import { HttpRequest, HttpHandlerFn, HttpResponse } from '@angular/common/http';
import { of } from 'rxjs';
import { describe, it, expect, beforeEach, vi } from 'vitest';

import { withCredentialsInterceptor } from './with-credentials.interceptor';
import { ConfigService } from '../services/config.service';

describe('withCredentialsInterceptor', () => {
  let config: { appApiUrl: ReturnType<typeof vi.fn> };

  function runInterceptor(req: HttpRequest<unknown>): HttpRequest<unknown> {
    let captured!: HttpRequest<unknown>;
    const next: HttpHandlerFn = (forwarded) => {
      captured = forwarded;
      return of(new HttpResponse({ status: 200 }));
    };
    TestBed.runInInjectionContext(() => {
      withCredentialsInterceptor(req, next).subscribe();
    });
    return captured;
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    config = { appApiUrl: vi.fn().mockReturnValue('http://localhost:8000') };
    TestBed.configureTestingModule({
      providers: [{ provide: ConfigService, useValue: config }],
    });
  });

  it('flips withCredentials on absolute app-api URLs', () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/models');
    const forwarded = runInterceptor(req);
    expect(forwarded.withCredentials).toBe(true);
  });

  it('flips withCredentials on same-origin /api requests when configured', () => {
    config.appApiUrl.mockReturnValue('/api');
    const req = new HttpRequest('GET', '/api/models');
    const forwarded = runInterceptor(req);
    expect(forwarded.withCredentials).toBe(true);
  });

  it('passes non-app-api URLs through untouched', () => {
    const req = new HttpRequest('GET', 'https://example.com/things');
    const forwarded = runInterceptor(req);
    expect(forwarded).toBe(req);
    expect(forwarded.withCredentials).toBe(false);
  });

  it('passes requests through when appApiUrl is unset', () => {
    config.appApiUrl.mockReturnValue('');
    const req = new HttpRequest('GET', 'http://localhost:8000/models');
    const forwarded = runInterceptor(req);
    expect(forwarded).toBe(req);
    expect(forwarded.withCredentials).toBe(false);
  });
});
