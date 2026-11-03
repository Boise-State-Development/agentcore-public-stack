import { TestBed } from '@angular/core/testing';
import {
  HttpErrorResponse,
  HttpHandlerFn,
  HttpHeaders,
  HttpRequest,
  HttpResponse,
} from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GATEWAY_RETRY_DELAY_MS, gatewayRetryInterceptor } from './retry.interceptor';

type Outcome = { response?: HttpResponse<unknown>; error?: unknown };

/**
 * Runs the interceptor against a handler whose answers are scripted in order,
 * driving the retry delay with fake timers. Returns the terminal outcome and
 * how many times the handler was asked.
 */
async function run(
  req: HttpRequest<unknown>,
  answers: Array<HttpResponse<unknown> | HttpErrorResponse>,
): Promise<Outcome & { calls: number }> {
  const next: HttpHandlerFn = vi.fn(() => {
    const answer = answers.shift();
    if (!answer) throw new Error('handler asked more times than scripted');
    return answer instanceof HttpErrorResponse ? throwError(() => answer) : of(answer);
  });

  const outcome: Outcome = {};
  let settled = false;
  TestBed.runInInjectionContext(() => {
    gatewayRetryInterceptor(req, next).subscribe({
      next: (event) => {
        if (event instanceof HttpResponse) outcome.response = event;
      },
      error: (error) => {
        outcome.error = error;
        settled = true;
      },
      complete: () => {
        settled = true;
      },
    });
  });

  // Anything that is going to retry waits exactly this long first.
  await vi.advanceTimersByTimeAsync(GATEWAY_RETRY_DELAY_MS);
  expect(settled).toBe(true);
  return { ...outcome, calls: (next as ReturnType<typeof vi.fn>).mock.calls.length };
}

const ok = () => new HttpResponse({ status: 200, body: { fine: true } });
const gateway = (status: number) =>
  new HttpErrorResponse({ status, url: 'http://localhost:8000/sessions' });

describe('gatewayRetryInterceptor', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('retries a GET once after a 502 and returns the second answer', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const result = await run(req, [gateway(502), ok()]);

    expect(result.calls).toBe(2);
    expect(result.error).toBeUndefined();
    expect(result.response?.status).toBe(200);
  });

  it.each([503, 504])('also retries a GET once after a %i', async (status) => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const result = await run(req, [gateway(status), ok()]);

    expect(result.calls).toBe(2);
    expect(result.response?.status).toBe(200);
  });

  it('waits before retrying rather than hammering the gateway', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const next: HttpHandlerFn = vi
      .fn()
      .mockReturnValueOnce(throwError(() => gateway(502)))
      .mockReturnValueOnce(of(ok()));

    TestBed.runInInjectionContext(() => {
      gatewayRetryInterceptor(req, next).subscribe();
    });

    await vi.advanceTimersByTimeAsync(GATEWAY_RETRY_DELAY_MS - 1);
    expect(next).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(next).toHaveBeenCalledTimes(2);
  });

  it('does not retry a POST, even on a 502', async () => {
    const req = new HttpRequest('POST', 'http://localhost:8000/sessions', { title: 'x' });
    const result = await run(req, [gateway(502)]);

    expect(result.calls).toBe(1);
    expect(result.error).toBeInstanceOf(HttpErrorResponse);
    expect((result.error as HttpErrorResponse).status).toBe(502);
  });

  it.each(['PUT', 'PATCH', 'DELETE'])('does not retry a %s', async (method) => {
    const req = new HttpRequest(method, 'http://localhost:8000/sessions/1', null);
    const result = await run(req, [gateway(503)]);

    expect(result.calls).toBe(1);
    expect((result.error as HttpErrorResponse).status).toBe(503);
  });

  it('does not retry twice: a second gateway error is surfaced', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const result = await run(req, [gateway(502), gateway(504)]);

    expect(result.calls).toBe(2);
    expect(result.response).toBeUndefined();
    expect((result.error as HttpErrorResponse).status).toBe(504);
  });

  it.each([400, 401, 403, 404, 409, 429])('passes a %i through untouched, with no retry', async (status) => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const error = gateway(status);
    const result = await run(req, [error]);

    expect(result.calls).toBe(1);
    expect(result.error).toBe(error);
  });

  it('passes a 500 through: the task answered, it just failed', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const result = await run(req, [gateway(500)]);

    expect(result.calls).toBe(1);
    expect((result.error as HttpErrorResponse).status).toBe(500);
  });

  it('never retries the chat stream endpoint', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/chat/stream?x=1');
    const result = await run(req, [gateway(502)]);

    expect(result.calls).toBe(1);
    expect((result.error as HttpErrorResponse).status).toBe(502);
  });

  it('never retries a request that asked for an event stream', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/notifications/feed', {
      headers: new HttpHeaders({ Accept: 'text/event-stream' }),
    });
    const result = await run(req, [gateway(504)]);

    expect(result.calls).toBe(1);
    expect((result.error as HttpErrorResponse).status).toBe(504);
  });

  it('leaves a successful GET alone', async () => {
    const req = new HttpRequest('GET', 'http://localhost:8000/sessions');
    const result = await run(req, [ok()]);

    expect(result.calls).toBe(1);
    expect(result.response?.status).toBe(200);
  });
});
