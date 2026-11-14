import { TestBed } from '@angular/core/testing';
import { HttpClient, HttpErrorResponse, provideHttpClient, withInterceptors } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ErrorService } from '../services/error/error.service';
import { ConfigService } from '../services/config.service';
import { csrfInterceptor } from './csrf.interceptor';
import { errorInterceptor } from './error.interceptor';
import { GATEWAY_RETRY_DELAY_MS, gatewayRetryInterceptor } from './retry.interceptor';
import { SessionService } from './session.service';
import { withCredentialsInterceptor } from './with-credentials.interceptor';

/**
 * The app's HTTP pipeline end to end over Angular's real FetchBackend, with
 * only `globalThis.fetch` scripted. The interceptor specs run against a stub
 * handler and HttpTestingController replaces the backend, so neither can see
 * what the backend itself does: what reaches `fetch()`, and what shape a
 * failure comes back in. These are the facts the interceptors and the error
 * toast depend on, pinned for the backend app.config actually uses.
 */
describe('HTTP pipeline over FetchBackend', () => {
  let http: HttpClient;
  let errorService: ErrorService;
  let api: string;
  const sessionService = {
    csrfHeaders: () => ({ 'X-CSRF-Token': 'csrf-test' }),
    handleUnauthorized: vi.fn(() => true),
  };
  let fetchMock: ReturnType<typeof vi.fn>;
  const realFetch = globalThis.fetch;

  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  beforeEach(() => {
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        // The same chain, in the same order, as app.config.ts.
        provideHttpClient(
          withInterceptors([
            withCredentialsInterceptor,
            csrfInterceptor,
            errorInterceptor,
            gatewayRetryInterceptor,
          ]),
        ),
        { provide: SessionService, useValue: sessionService },
      ],
    });
    http = TestBed.inject(HttpClient);
    errorService = TestBed.inject(ErrorService);
    api = TestBed.inject(ConfigService).appApiUrl();
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  const initOf = (call: number) => fetchMock.mock.calls[call][1] as RequestInit;

  it('sends app-api requests with credentials so the BFF cookie is attached', async () => {
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));

    await firstValueFrom(http.get(`${api}/sessions`));

    expect(initOf(0).credentials).toBe('include');
  });

  it('does not send credentials to other hosts', async () => {
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));

    await firstValueFrom(http.get('https://example.test/thing'));

    expect(initOf(0).credentials).toBeUndefined();
  });

  it('puts the CSRF header on unsafe methods', async () => {
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));

    await firstValueFrom(http.post(`${api}/sessions`, { title: 'x' }));

    expect((initOf(0).headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-test');
  });

  it('retries a GET once on a gateway error and returns the second answer', async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(json(502, { detail: 'bad gateway' }))
      .mockResolvedValueOnce(json(200, { sessions: [] }));

    const result = firstValueFrom(http.get(`${api}/sessions`));
    await vi.advanceTimersByTimeAsync(GATEWAY_RETRY_DELAY_MS);

    await expect(result).resolves.toEqual({ sessions: [] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(errorService.errorMessages()).toHaveLength(0);
  });

  it('shows the server detail from a JSON error body', async () => {
    fetchMock.mockResolvedValueOnce(json(403, { detail: 'You do not have access to this tool.' }));

    const error = await firstValueFrom(http.get(`${api}/tools/x`)).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(HttpErrorResponse);
    expect((error as HttpErrorResponse).status).toBe(403);
    expect(errorService.errorMessages()[0]?.message).toBe('You do not have access to this tool.');
  });

  it('reports a network failure as status 0 with the generic message, without retrying', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    const error = await firstValueFrom(http.get(`${api}/sessions`)).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(HttpErrorResponse);
    expect((error as HttpErrorResponse).status).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(errorService.errorMessages()[0]?.message).toBe('An error occurred. Please try again.');
  });

  it('routes a 401 to the login redirect instead of a toast', async () => {
    fetchMock.mockResolvedValueOnce(json(401, { detail: 'Not authenticated' }));

    await firstValueFrom(http.get(`${api}/sessions`)).catch(() => undefined);

    expect(sessionService.handleUnauthorized).toHaveBeenCalled();
    expect(errorService.errorMessages()).toHaveLength(0);
  });
});
