import { HttpErrorResponse, HttpInterceptorFn, HttpRequest } from '@angular/common/http';
import { defer, retry, throwError, timer } from 'rxjs';

/** How long to wait before the one retry. */
export const GATEWAY_RETRY_DELAY_MS = 750;

/**
 * Statuses the ALB returns when the task it picked is gone or not answering:
 * 502 (connection refused or reset by a dead task), 503 (no healthy target),
 * 504 (the task accepted the connection and never answered).
 */
const GATEWAY_ERROR_STATUSES = new Set([502, 503, 504]);

/**
 * SSE endpoints. `fetchEventSource` carries the chat stream outside the
 * HttpClient pipeline, so this is belt and braces; the Accept-header check
 * below covers anything else that opens a stream through HttpClient.
 */
const STREAMING_ENDPOINTS = ['/chat/stream'];

export function isStreamingRequest(req: HttpRequest<unknown>): boolean {
  if (STREAMING_ENDPOINTS.some((endpoint) => req.url.includes(endpoint))) {
    return true;
  }
  const accept = req.headers.get('Accept') ?? '';
  return accept.includes('text/event-stream');
}

function isGatewayError(error: unknown): error is HttpErrorResponse {
  return error instanceof HttpErrorResponse && GATEWAY_ERROR_STATUSES.has(error.status);
}

/**
 * Retries a GET **once**, after a short pause, when the response was a
 * gateway error (502, 503, 504).
 *
 * app-api runs three Fargate tasks behind an ALB. When one task dies or hangs,
 * the ALB keeps routing a share of requests to it until two health probes fail
 * (about 20 s), and each of those requests comes back as a 502 or 504. The ALB
 * round-robins across targets, so the same request sent again lands on a
 * healthy task almost every time. One retry turns that window into nothing
 * the user sees on a page load.
 *
 * Only GETs: a POST, PUT, PATCH or DELETE that got a 504 may have been applied
 * before the gateway gave up, and sending it twice is worse than surfacing the
 * error. Never for SSE: a stream has its own reconnect handling in
 * chat-http.service.ts, and a retry here would open a second one.
 *
 * Sits after errorInterceptor in the chain (closer to the backend), so the
 * error toast and the 401 redirect only ever see the outcome of the retry.
 */
export const gatewayRetryInterceptor: HttpInterceptorFn = (req, next) => {
  if (req.method !== 'GET' || isStreamingRequest(req)) {
    return next(req);
  }

  // `defer` so the retry asks the downstream chain for a fresh request rather
  // than resubscribing to whatever the first call returned.
  return defer(() => next(req)).pipe(
    retry({
      count: 1,
      delay: (error: unknown) =>
        isGatewayError(error) ? timer(GATEWAY_RETRY_DELAY_MS) : throwError(() => error),
    }),
  );
};
