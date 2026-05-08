# Bugfix Requirements Document

## Introduction

Since the `v1.0.0-beta.24` BFF Token Handler release (commit `258193d`, deployed 2026-05-06), the app-api service has exhibited severe tail-latency and ingress stalls on page loads. Angular's refresh fan-out (~8 concurrent endpoints — `/auth/session`, `/models`, `/tools/`, `/files/quota`, `/users/me/permissions`, `/sessions`, `/assistants`, `/connectors/`) sees requests time out or exceed the ALB 60s idle cap. Observed signals over the last 24h on the affected fleet:

- Two `HTTPCode_ELB_504_Count` events (13:37 and 14:40 UTC) — driven by ALB idle timeout, not target 5xx.
- `TargetResponseTime` p-max of 15.6s at 15:25 UTC; `/files/quota` outliers reaching ~80s.
- Endpoint p95s under load: `/models` ~141ms, `/tools/` ~289ms, `/users/me/permissions` ~239ms, `/sessions` ~188ms.
- ECS task at 0.7% CPU / 23% memory. No DDB throttling (0 `ReadThrottleEvents` / `WriteThrottleEvents` across all 23 tables). Zero target 5xx.

The new `SessionRefreshMiddleware` runs on every request carrying a `__Host-bff_session` cookie. Its hot-path calls block the single-worker uvicorn event loop on synchronous boto3 I/O (DynamoDB + Cognito), its cache TTL and its sliding-renewal throttle are aligned on the same 60s boundary, and the per-session lock that coalesces refreshes does not coalesce the broader session-resolve path. The result is ~16 serialized blocking AWS calls at the front of every page load per active user, once per minute — with no concurrency slack because the service runs one uvicorn worker in one ECS task.

Impact: degraded UX for every logged-in user (spinners, stale data, failed tab refreshes), 504s visible to users, and a fragile service posture where any single slow AWS call stalls every in-flight request on the same task.

## Bug Analysis

### Current Behavior (Defect)

What currently happens under the new middleware on cookie-bearing requests:

1.1 WHEN `SessionRepository.get`, `touch_last_seen`, `update_tokens`, `put`, or `delete` is awaited inside a request handler THEN the uvicorn event loop blocks for the full DynamoDB round-trip because the methods are declared `async def` but call boto3 synchronously with no `asyncio.to_thread` offload and no aioboto3.

1.2 WHEN `SessionRefreshMiddleware._resolve_session` invokes `CognitoRefreshClient.refresh` THEN the uvicorn event loop blocks for the full `cognito-idp:initiate_auth` round-trip because `CognitoRefreshClient.refresh` is a plain `def` called without threadpool offload, and it runs while the per-session `asyncio.Lock` from `get_session_lock()` is held.

1.3 WHEN N concurrent requests for the same `session_id` arrive with no valid cached `SessionRecord` THEN the middleware issues N independent DynamoDB `get_item` calls because the existing per-session lock only coalesces the refresh exchange, not the upstream unseal → `SessionCache.get` → `SessionRepository.get` sequence.

1.4 WHEN the `SessionCache` TTL elapses at the same moment the sliding-renewal throttle window elapses THEN a single request triggers both a DynamoDB `get_item` and a DynamoDB `update_item` on its critical path because `_DEFAULT_REFRESH_LEEWAY_SECONDS` and `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` are both `60` in `sessions_bff/config.py`, so cache expiry and throttle expiry are aligned.

1.5 WHEN a request passes `SessionRefreshMiddleware` and a slide is warranted THEN the caller's response waits for `touch_last_seen` to complete against DynamoDB because `_maybe_slide` `await`s the write inline on the request path, even though the code is documented to swallow failures ("Don't fail the request if the slide-write fails").

1.6 WHEN the app-api container starts THEN the service has no concurrency slack because the `CMD` in `backend/Dockerfile.app-api` launches a single uvicorn worker with no `--workers` flag and the service runs as a single ECS task — one blocked event loop stalls all ingress, consistent with low CPU/memory while latency climbs.

1.7 WHEN Angular fires its ~8-endpoint page-load fan-out with a session cookie and the per-session cache window has just elapsed THEN ~16 serialized blocking DynamoDB operations (per-coroutine `get_item` plus per-coroutine `update_item`) accumulate at the front of the page load because each coroutine independently observes cache-miss and past-throttle on its local `SessionRecord` copy and each runs its own blocking AWS I/O on the event loop thread.

### Expected Behavior (Correct)

What should happen instead, keeping the same middleware surface and contracts:

2.1 WHEN `SessionRepository.get`, `touch_last_seen`, `update_tokens`, `put`, or `delete` is awaited inside a request handler THEN the method SHALL execute its underlying boto3 call in a threadpool (via `asyncio.to_thread` or an equivalent offload) so the uvicorn event loop continues scheduling other coroutines for the full DynamoDB round-trip.

2.2 WHEN `SessionRefreshMiddleware._resolve_session` invokes `CognitoRefreshClient.refresh` THEN the Cognito `initiate_auth` call SHALL execute in a threadpool so the uvicorn event loop continues scheduling other coroutines — including coroutines for different `session_id`s — while the per-session `asyncio.Lock` is held.

2.3 WHEN N concurrent requests for the same `session_id` arrive with no valid cached `SessionRecord` THEN the middleware SHALL coalesce them so at most one DynamoDB `get_item` is issued per `session_id` per cache window; the remaining N−1 requests SHALL await a shared in-process future keyed by `session_id` and consume its result.

2.4 WHEN the `SessionCache` TTL elapses THEN a cache miss SHALL NOT imply a sliding-renewal DynamoDB write on the same request because `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` SHALL be a strict multiple of `_DEFAULT_REFRESH_LEEWAY_SECONDS` (e.g. 300s versus 60s) so cache-expiry and throttle-expiry are de-aligned.

2.5 WHEN a request passes `SessionRefreshMiddleware` and a slide is warranted THEN the caller's response SHALL NOT wait for `touch_last_seen` because `_maybe_slide` SHALL dispatch the DynamoDB write as a detached `asyncio.Task` (fire-and-forget) and SHALL return the computed `Max-Age` to the response path immediately.

2.6 WHEN the app-api container starts THEN the service SHALL have concurrency slack such that a single blocked event loop does not stall all ingress — uvicorn SHALL run with `--workers >= 2` (at current `cpu=512`) and/or the ECS service SHALL run `>= 2` tasks; the chosen configuration SHALL be deployed.

2.7 WHEN Angular fires its ~8-endpoint page-load fan-out with a session cookie and the per-session cache window has just elapsed THEN the middleware SHALL issue at most 1 DynamoDB `get_item` and at most 1 DynamoDB `update_item` per `session_id` per cache window across the fan-out (not ~16 blocking calls), and all 8 responses SHALL return without any one of them serially waiting on another's AWS I/O to complete.

### Unchanged Behavior (Regression Prevention)

Existing guarantees that MUST continue to hold after the fix:

3.1 WHEN `BFFConfig.is_enabled()` returns `False` (env vars unset) THEN `SessionRefreshMiddleware` SHALL CONTINUE TO short-circuit as a pass-through with no AWS calls (dormant pass-through invariant preserved).

3.2 WHEN a request arrives without a `__Host-bff_session` cookie THEN `SessionRefreshMiddleware` SHALL CONTINUE TO pass the request through without unsealing, cache lookup, or DynamoDB access.

3.3 WHEN an unrecoverable cookie is detected (bad seal, missing DynamoDB row, expired TTL, or terminal `CognitoRefreshError`) THEN the middleware SHALL CONTINUE TO clear both `__Host-bff_session` and `__Host-bff_csrf` on the response.

3.4 WHEN `_maybe_slide` returns a non-`None` `Max-Age` THEN the response's `Set-Cookie` headers for `__Host-bff_session` and `__Host-bff_csrf` SHALL CONTINUE TO use that exact value (the cookie re-emit contract between `_maybe_slide` and `_reemit_cookies` is preserved under fire-and-forget dispatch of the DynamoDB write).

3.5 WHEN N concurrent requests for the same `session_id` cross the refresh-leeway window at the same moment THEN exactly one `cognito-idp:initiate_auth` exchange SHALL CONTINUE TO be issued per `session_id` per leeway window (the existing refresh-storm coalescing via `get_session_lock(session_id)` is preserved end-to-end).

3.6 WHEN `CookieCodec._ensure_cipher` is called on a hot request THEN the AES-GCM cipher SHALL CONTINUE TO be served from the process-wide `get_default_codec()` singleton with no per-request `kms:GenerateDataKey` call.

3.7 WHEN `resolve_bff_client_secret` is called on a hot request THEN the BFF Cognito app-client secret SHALL CONTINUE TO be served from the module-scope cache with no per-request `secretsmanager:GetSecretValue` call.

3.8 WHEN `CSRFMiddleware` validates an unsafe-method request with `request.state.bff_session` set THEN it SHALL CONTINUE TO accept/reject using the existing in-memory HMAC double-submit check with no new I/O introduced on its path.

3.9 WHEN the absolute-lifetime cap (`created_at + absolute_lifetime_seconds`) has passed THEN `_maybe_slide` SHALL CONTINUE TO return `None` so no further cookie re-emission or DynamoDB slide is issued.

3.10 WHEN a refresh rotates the Cognito refresh token and the `update_tokens` persist fails THEN the middleware SHALL CONTINUE TO invalidate the cache entry and clear the cookie so the user is forced to re-authenticate (fail-closed rotation behavior preserved).

3.11 WHEN the BFF cookie seal fails to decode THEN the middleware SHALL CONTINUE TO treat every decode failure identically (no timing or response-shape oracle introduced by the new offload or single-flight paths).
