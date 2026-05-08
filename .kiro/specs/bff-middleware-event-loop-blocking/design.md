# BFF Middleware Event Loop Blocking Bugfix Design

## Overview

The `SessionRefreshMiddleware` runs on every cookie-bearing request and, as of `v1.0.0-beta.24`, executes four independent classes of blocking/serialized work on the uvicorn event loop:

1. **Sync boto3 I/O on the event loop thread** — `SessionRepository.*` and `CognitoRefreshClient.refresh` are declared `async def` but call boto3 synchronously. Every DynamoDB `get_item`/`update_item` and every Cognito `initiate_auth` freezes the whole event loop for its round-trip duration.
2. **Missing fan-out coalescing** — the per-session `asyncio.Lock` wraps only the refresh exchange. The upstream `unseal → cache → get_item → maybe_slide` path is not coalesced, so Angular's ~8-endpoint page-load fan-out produces ~16 serialized blocking DDB calls per cache window.
3. **Aligned cache TTL / throttle window** — `_DEFAULT_REFRESH_LEEWAY_SECONDS` and `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` both default to 60s. Cache expiry and slide-throttle expiry land on the same boundary, so a single request crossing that boundary incurs both a `get_item` and an `update_item` on its critical path.
4. **Inline awaited slide-write** — `_maybe_slide` awaits `touch_last_seen` on the request path even though the call is already written defensively (failures are swallowed). The caller's response waits on DDB.

All of this runs inside a **single uvicorn worker on a single ECS task** (no `--workers` flag in `backend/Dockerfile.app-api`, `desiredCount: 1` in CDK), so any one blocked round-trip stalls every other in-flight request.

The fix is a targeted, minimal-surface intervention that keeps the middleware's public contracts intact:

- Offload every synchronous boto3 call in `SessionRepository` and `CognitoRefreshClient.refresh` via `asyncio.to_thread`.
- Introduce a per-session `asyncio.Future`-based single-flight in front of the `get_item → needs_refresh → maybe-refresh` path so N concurrent requests for the same `session_id` share one lookup result.
- De-align `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` from the cache/leeway window (raise to 300s) so cache-miss does not imply slide-write.
- Dispatch `_maybe_slide`'s `touch_last_seen` as a detached `asyncio.Task` and return the `Max-Age` synchronously.
- Add concurrency slack at the deployment layer (raise `CDK_APP_API_DESIRED_COUNT` to ≥ 2 for production config, keeping 1 valid for dev) so a single stuck event loop can no longer halt all ingress.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — a cookie-bearing request reaches `SessionRefreshMiddleware` while the middleware is active (`BFFConfig.is_enabled()` is True), under any of the sub-conditions 1.1–1.7 in `bugfix.md#Current Behavior`.
- **Property (P)**: The desired behavior when the bug condition holds — AWS I/O never freezes the uvicorn event loop, fan-outs share a single coalesced lookup, and slide-writes never block the response path.
- **Preservation**: Existing contracts that must remain unchanged — dormant pass-through (`is_enabled() == False`), no-cookie pass-through, unrecoverable-cookie clearing, refresh-storm coalescing, Max-Age re-emit contract, CSRF unchanged, absolute-lifetime cap, fail-closed rotation, uniform cookie decode failure.
- **SessionRefreshMiddleware**: The middleware in `backend/src/apis/shared/middleware/session_refresh.py` that unseals the BFF cookie, resolves the `SessionRecord`, optionally refreshes Cognito tokens, and slides the session's DDB TTL.
- **SessionRepository**: The repository in `backend/src/apis/shared/sessions_bff/repository.py` that wraps boto3 DynamoDB calls with `async def` signatures. Today the methods call boto3 synchronously on the event loop thread.
- **CognitoRefreshClient**: The class in `backend/src/apis/shared/sessions_bff/refresh.py` whose `refresh()` method is plain `def` and calls `cognito-idp:initiate_auth` synchronously.
- **SessionCache**: The process-wide `TTLCache` in `backend/src/apis/shared/sessions_bff/cache.py` whose TTL defaults to `refresh_leeway_seconds` (60s).
- **`_DEFAULT_REFRESH_LEEWAY_SECONDS`**: 60s constant in `config.py` — both the refresh pre-expiry window and the SessionCache TTL.
- **`_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS`**: 60s constant in `config.py` — the minimum interval between DDB `touch_last_seen` writes for a single session. Currently aligned with leeway, will be de-aligned to 300s.
- **per-session `asyncio.Lock`**: The lock from `get_session_lock(session_id)` in `sessions_bff/lock.py`. Today it wraps only the Cognito refresh exchange; the fix does NOT move its scope — a separate single-flight `Future` is added upstream.
- **Single-flight Future**: New per-session `asyncio.Future` added for this fix that coalesces the upstream `get_item → needs_refresh → refresh?` resolution across concurrent callers within one task.

## Bug Details

### Bug Condition

The bug manifests when a request reaches `SessionRefreshMiddleware.dispatch` with `BFFConfig.is_enabled() == True` AND a `__Host-bff_session` cookie present. Under this condition the middleware's resolve/slide path performs at least one event-loop-blocking AWS call, and — under fan-out — performs 2×N blocking calls for N concurrent same-session requests. The observable symptoms (504s, 80s `/files/quota` tails, 15.6s p-max at 0.7% CPU) follow directly.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type HTTPRequest
  OUTPUT: boolean

  # Middleware-level precondition — everything else is scoped inside this.
  IF NOT BFFConfig.from_env().is_enabled() THEN
    RETURN false
  END IF
  IF input.cookies["__Host-bff_session"] IS NULL THEN
    RETURN false
  END IF

  # Sub-condition 1.1: sync boto3 in SessionRepository blocks the loop.
  blocks_on_repo := (
    awaitedIn(request, SessionRepository.get)
      OR awaitedIn(request, SessionRepository.touch_last_seen)
      OR awaitedIn(request, SessionRepository.update_tokens)
      OR awaitedIn(request, SessionRepository.put)
      OR awaitedIn(request, SessionRepository.delete)
  )
    AND NOT executesInThreadpool(boto3_call_of_that_method)

  # Sub-condition 1.2: sync boto3 in CognitoRefreshClient blocks the loop,
  # AND it runs while get_session_lock(session_id) is held.
  blocks_on_cognito := (
    invokedIn(request, CognitoRefreshClient.refresh)
      AND NOT executesInThreadpool(initiate_auth_call)
      AND sessionLockHeldDuring(initiate_auth_call)
  )

  # Sub-condition 1.3: N concurrent same-session requests are not coalesced
  # across the session-resolve path.
  missing_resolve_coalescing := (
    concurrentRequestsForSameSession(input.session_id) > 1
      AND countOf(SessionRepository.get calls for input.session_id in this window)
          = concurrentRequestsForSameSession(input.session_id)
  )

  # Sub-condition 1.4: cache-miss boundary aligns with throttle boundary.
  aligned_windows := (
    BFFConfig._DEFAULT_REFRESH_LEEWAY_SECONDS
      == BFFConfig._DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS
  )

  # Sub-condition 1.5: response waits on inline-awaited touch_last_seen.
  inline_slide := (
    slideWarrantedFor(request)
      AND responseWaitsFor(touch_last_seen_call_of_this_request)
  )

  # Sub-condition 1.6: no concurrency slack at the deployment boundary.
  no_slack := (
    uvicornWorkerCount() == 1
      AND ecsDesiredCount() == 1
  )

  # Sub-condition 1.7: page-load fan-out amplifies 1.1 + 1.3 + 1.4.
  amplified_fanout := (
    concurrentRequestsForSameSession(input.session_id) >= 8
      AND cacheWindowJustElapsedFor(input.session_id)
      AND countOf(DDB calls on critical path during this window)
          >= 2 * concurrentRequestsForSameSession(input.session_id)
  )

  RETURN blocks_on_repo
    OR blocks_on_cognito
    OR missing_resolve_coalescing
    OR aligned_windows
    OR inline_slide
    OR no_slack
    OR amplified_fanout
END FUNCTION
```

### Examples

- **1.1 blocking repo call**: Any request that hits `request.state.bff_session = record` → `_maybe_slide` → `touch_last_seen`. Expected: the DDB round-trip runs off the event loop thread; other coroutines continue to be scheduled. Actual: the event loop is frozen for the full round-trip.
- **1.2 blocking Cognito call**: Two tabs refresh concurrently at minute 59 of the access token's lifetime. Expected: the Cognito `initiate_auth` for session A runs off the loop thread; unrelated requests (different cookies, Bearer-token requests, health checks) proceed. Actual: the loop is frozen for the full Cognito round-trip AND the per-session lock is held during that freeze.
- **1.3 missing resolve coalescing**: Angular fan-out of 8 same-session requests with no cached `SessionRecord`. Expected: 1 DDB `get_item`. Actual: 8 DDB `get_item` calls, each blocking.
- **1.4 aligned windows**: A request at T when `T - last_seen_at == 60s` AND `SessionCache` entry for this session has just TTL-evicted at T. Expected: at most 1 of `{get_item, update_item}`. Actual: both, serialized.
- **1.5 inline slide**: Request with `_maybe_slide` returning non-None. Expected: the response Set-Cookie lands immediately; the DDB write happens in the background. Actual: the response waits for DDB.
- **1.7 page-load fan-out**: Angular page load fires 8 endpoints at once right after a cache window elapses. Expected: ≤1 `get_item` + ≤1 `update_item` across the 8 requests. Actual: up to 16 serialized blocking calls at the front of the page load.
- **Edge case — `is_enabled() == False`**: The middleware must short-circuit before any of the above sub-conditions can manifest. No AWS calls, no locks, no futures.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- **3.1 Dormant pass-through**: `BFFConfig.is_enabled() == False` → `dispatch` short-circuits to `call_next(request)` with no AWS calls, no cache lookup, no single-flight registration.
- **3.2 No-cookie pass-through**: No `__Host-bff_session` cookie → same short-circuit as 3.1.
- **3.3 Unrecoverable cookie → clear both cookies**: Bad seal, missing DDB row, expired TTL, or terminal `CognitoRefreshError` → `_clear_cookies(response)` clears both `__Host-bff_session` AND `__Host-bff_csrf` with the same attribute set as today.
- **3.4 Max-Age re-emit contract**: When `_maybe_slide` returns a non-None `Max-Age`, the `Set-Cookie` headers for both BFF cookies use that exact value and the exact attribute set in `_reemit_cookies` today. Fire-and-forget dispatch of the DDB write does not change this contract.
- **3.5 Refresh-storm coalescing (existing)**: For N concurrent same-session requests crossing the refresh-leeway boundary, exactly one `cognito-idp:initiate_auth` is issued per `session_id` per leeway window. The existing `get_session_lock(session_id)` scope around the Cognito exchange is preserved end-to-end.
- **3.6 Codec singleton**: `get_default_codec()` is the same process-wide instance used by the auth/callback seal path and the middleware unseal path. No per-request `kms:GenerateDataKey` is introduced.
- **3.7 Client-secret cache**: `resolve_bff_client_secret` continues to serve from the module-scope cache. No per-request `secretsmanager:GetSecretValue`.
- **3.8 CSRF middleware path**: `CSRFMiddleware` continues to validate unsafe-method requests using the existing in-memory HMAC double-submit check against `request.state.bff_csrf_token`. No new I/O is introduced on that path.
- **3.9 Absolute-lifetime cap**: `_maybe_slide` returns `None` once `created_at + absolute_lifetime_seconds` has passed. No further cookie re-emit or DDB slide.
- **3.10 Fail-closed rotation**: When Cognito rotates the refresh token and `_persist_refresh` exhausts its retries, the middleware invalidates the cache and clears the cookie.
- **3.11 Uniform cookie decode failure**: Every `CookieDecodeError` branch produces the same response shape and timing signature. No new oracle is introduced by the offload or single-flight paths.

**Scope:**

All inputs that do NOT involve the BFF middleware path should be completely unaffected by this fix. This includes:

- Bearer-token requests (no `__Host-bff_session` cookie) — untouched.
- Anonymous endpoints (health, static assets) — untouched.
- WebSocket voice routes — they replicate the cookie unseal + DDB lookup outside the middleware (see `voice/routes.py`); this fix does not change their path.
- The auth/callback token-exchange route — it uses the same `CookieCodec` singleton to seal cookies; the singleton is not disturbed.
- The logout route — its cache `invalidate(session_id)` call is preserved.

## Hypothesized Root Cause

Based on the bug description and code inspection, the root causes are concurrent and independent — each sub-condition has its own root cause, and the fix addresses all of them:

1. **Sync boto3 in `async def` methods (1.1, 1.2)**: The `SessionRepository` docstring explicitly acknowledges this ("The methods are declared `async` to match the rest of `apis.shared`, but boto3 is sync — calls run on the event loop thread"). The original reasoning was that refresh-storm coalescing via `get_session_lock()` would hold fan-out low enough to make thread-pool offload unnecessary. That reasoning is wrong for two reasons: (a) the lock only covers the Cognito exchange, not the DDB path — so fan-out is not coalesced at all for cache misses; and (b) even a single blocking call is enough to freeze the event loop for the round-trip duration, which is directly observable in `TargetResponseTime` p-max.

2. **Wrong lock scope (1.3, 1.7)**: `get_session_lock(session_id)` is acquired inside `_resolve_session` only after the `_cache.get → _repository.get → needs_refresh` decision has been made. An `asyncio.Lock` held this narrowly cannot coalesce anything upstream of itself. The fix needs a different primitive — an `asyncio.Future` stored in a per-session slot that N waiters can await — because a lock would serialize N requests through one DDB call instead of consolidating them to one call.

3. **Aligned windows by default (1.4)**: Both constants default to 60s in `config.py`. A strict-multiple relationship (e.g. throttle = 5 × leeway) de-aligns the boundaries. This is a config fix with no code change needed in the middleware.

4. **`await` on `touch_last_seen` by pattern (1.5)**: `_maybe_slide` awaits the write because that matches the rest of the codebase's DB access shape. The surrounding `try/except` already swallows failures (documented as "Don't fail the request if the slide-write fails"), which is exactly the pre-condition that makes fire-and-forget safe.

5. **Single-worker container (1.6)**: The Dockerfile CMD ships one uvicorn worker and `desiredCount: 1` in CDK ships one task. This was fine for the Bearer-token era; under the BFF middleware, it means any one blocked round-trip halts every other in-flight request. Concurrency slack is a separate lever from event-loop non-blocking — both are required, neither is sufficient alone.

## Correctness Properties

Property 1: Bug Condition — Event-Loop Non-Blocking, Coalesced, Window-Staggered, Fire-and-Forget BFF Middleware

_For any_ request where the bug condition holds (`isBugCondition` returns true), the fixed middleware and its collaborators SHALL (a) execute every boto3 DynamoDB and Cognito call off the event loop thread (via `asyncio.to_thread` or equivalent), (b) coalesce N concurrent same-`session_id` requests crossing a cold cache window to at most one DynamoDB `get_item` via a per-session `asyncio.Future`, (c) hold the `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` default to a strict multiple of `_DEFAULT_REFRESH_LEEWAY_SECONDS` (300s vs 60s) so cache-expiry and throttle-expiry do not align, (d) dispatch `_maybe_slide`'s `touch_last_seen` as a detached `asyncio.Task` and return the `Max-Age` to the response path synchronously, and (e) run with concurrency slack such that `desiredCount >= 2` in production configuration. The observable result SHALL be that Angular's ~8-endpoint page-load fan-out issues at most 1 `get_item` and at most 1 `update_item` per `session_id` per cache window (not ~16), and no single AWS call serializes unrelated requests.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**

Property 2: Preservation — BFF Middleware Contracts Unchanged for Non-Buggy Inputs

_For any_ request where the bug condition does NOT hold (`isBugCondition` returns false), the fixed middleware SHALL produce the same externally observable result as the original middleware, preserving: dormant pass-through (`is_enabled() == False`), no-cookie pass-through, unrecoverable-cookie clearing of both `__Host-bff_session` and `__Host-bff_csrf` with the same attribute set, the `Max-Age` re-emit contract between `_maybe_slide` and `_reemit_cookies`, exactly-one Cognito `initiate_auth` per `session_id` per leeway window, the `CookieCodec` and client-secret process-wide singletons, the `CSRFMiddleware` in-memory HMAC double-submit check, the absolute-lifetime cap behavior, fail-closed refresh-token rotation, and uniform `CookieDecodeError` handling.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11**

## Fix Implementation

### Changes Required

Assuming the root cause analysis above is correct, the fix spans four code locations and one infrastructure config.

**File**: `backend/src/apis/shared/sessions_bff/repository.py`

**Function**: `SessionRepository.get`, `touch_last_seen`, `update_tokens`, `put`, `delete`

**Specific Changes**:

1. **Threadpool offload for every boto3 call**: Extract each method's boto3 invocation into a nested sync helper and invoke it via `await asyncio.to_thread(helper, ...)`. Example for `get`:
   ```
   async def get(self, session_id):
       if not self._enabled:
           return None
       def _call():
           return self._table.get_item(Key=self._key(session_id))
       try:
           response = await asyncio.to_thread(_call)
       except ClientError as exc:
           ...
   ```
   The method signatures, return types, and exception handling stay identical. The post-decode TTL defense-in-depth check and `_item_to_record` translation stay on the calling coroutine.

2. **No change to public API**: Every callsite in the middleware (`self._repository.get`, `self._repository.touch_last_seen`, `self._repository.update_tokens`) remains an `await`. The offload is purely internal.

**File**: `backend/src/apis/shared/sessions_bff/refresh.py`

**Function**: `CognitoRefreshClient.refresh`

**Specific Changes**:

3. **Add async wrapper that offloads to a threadpool**: Either rename `refresh` to `_refresh_sync` and add a new `async def refresh(...)` that calls `await asyncio.to_thread(self._refresh_sync, username=..., refresh_token=...)`, or convert `refresh` to `async def` in-place with the same offload. The middleware callsite (`self._refresh_client.refresh(...)`) becomes `await self._refresh_client.refresh(...)`. The Cognito SDK call and the `CognitoRefreshError` contract are unchanged.

**File**: `backend/src/apis/shared/middleware/session_refresh.py`

**Function**: `SessionRefreshMiddleware._resolve_session`, `_maybe_slide`, `dispatch`

**Specific Changes**:

4. **Add per-session single-flight for the session-resolve path**: Introduce a new module-level `dict[str, asyncio.Future[tuple[Optional[SessionRecord], bool]]]` guarded by a thread lock in a new small module `backend/src/apis/shared/sessions_bff/single_flight.py` (mirroring `lock.py`'s shape), with an API:
   ```
   async def resolve_once(session_id, loader_coro_factory) -> tuple[Optional[SessionRecord], bool]
   ```
   The leader creates an `asyncio.Future`, registers it, runs the loader, sets the result/exception, and removes the entry. Followers `await` the existing Future. In `_resolve_session`, wrap the `_cache.get → _repository.get → needs_refresh → (maybe refresh)` block (from cache lookup through return) inside this single-flight, keyed by `session_id`. The existing `get_session_lock(session_id)` scope around the Cognito refresh exchange is **not** moved or widened — it stays exactly where it is today.

5. **Fire-and-forget slide-write in `_maybe_slide`**: Replace `await self._repository.touch_last_seen(...)` with a detached task. The function still computes `new_max_age` and returns it synchronously. The DDB write happens in the background; the existing `try/except` that was already documented to swallow failures moves into a `_slide_write_task(...)` helper that logs on failure. Update the local cache (`record.last_seen_at = now`, `record.ttl = new_ttl`, `self._cache.set(record)`) before scheduling the task, so subsequent same-request reads see the slid state.

6. **No change to `dispatch` structure or the cookie-clear / cookie-reemit branches**: Keep `clear_cookie` and `renewal_max_age` handling identical.

**File**: `backend/src/apis/shared/sessions_bff/config.py`

**Constant**: `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS`

**Specific Changes**:

7. **Raise default from 60s to 300s**: Change the constant from `60` to `60 * 5` (or explicitly `300`) so the cache TTL (tied to `_DEFAULT_REFRESH_LEEWAY_SECONDS = 60`) and the slide-throttle window are strict multiples. The env var `BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS` continues to override.

**File**: `infrastructure/cdk.context.json` (and test fixtures under `infrastructure/test/`)

**Key**: `appApi.desiredCount`

**Specific Changes**:

8. **Raise production `desiredCount` to 2**: Keep `maxCapacity` as-is (4). Update only the production/non-test context — test fixtures can stay at 1 if needed to keep CDK unit tests fast, but the top-level production context value must flip to 2. This is a **deployment-time** behavior change and the last item in the fix plan; it does not become necessary until the other changes ship.

**No changes required** in: `backend/src/apis/shared/sessions_bff/cache.py`, `backend/src/apis/shared/sessions_bff/cookie.py`, `backend/src/apis/shared/sessions_bff/lock.py`, `backend/src/apis/shared/sessions_bff/csrf.py`, `backend/src/apis/shared/middleware/csrf.py`, `backend/src/apis/app_api/auth/bff/*`, or the uvicorn `CMD` in `backend/Dockerfile.app-api` (the ECS `desiredCount` bump is the chosen vector for concurrency slack in 2.6 — a `--workers N` flag would require reworking the in-process singletons in `cache.py` and `refresh.py`, which is out of scope).

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior. Because four of the sub-conditions are independent, we run the exploratory phase against each one.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root-cause analysis for each sub-condition. If any is refuted, we re-hypothesize.

**Test Plan**: Write tests that inject a slow/instrumented boto3 stub (for DDB and Cognito) and drive the middleware directly under `pytest-asyncio`. For each sub-condition, assert the blocking/serialization behavior is present on unfixed code. Run on UNFIXED code first; the assertions SHALL fail against fixed code later.

**Test Cases**:

1. **Event loop blocked by `SessionRepository.get`** (validates 1.1): Stub the boto3 `table.get_item` with a 500ms `time.sleep`. Submit a `SessionRepository.get` call and a concurrent `asyncio.sleep(0.05)` marker coroutine on the same loop. Assert the marker resolves strictly after the `get` (will hold on unfixed code, will fail on fixed code where the marker completes long before `get` returns).

2. **Event loop blocked by `CognitoRefreshClient.refresh`** (validates 1.2): Same shape as (1) but against a stubbed `cognito-idp:initiate_auth`. Additionally assert that `get_session_lock(other_session_id)` can be acquired concurrently (will fail on unfixed code because the sync Cognito call has frozen the whole loop thread).

3. **N fan-out → N `get_item` calls** (validates 1.3): Spin up 8 concurrent `dispatch` calls with the same cookie and a cold `SessionCache`. Count `table.get_item` invocations on the stub. Assert count == 8 on unfixed code; the fix target is 1.

4. **Aligned windows → both writes on one request** (validates 1.4): Set clock to a moment where the cache TTL just elapsed AND `now - last_seen_at == 60s`. Drive a single request. Assert both `get_item` AND `update_item` are called on unfixed code; on fixed code with the new 300s throttle default, only `get_item` is called.

5. **Response waits on `touch_last_seen`** (validates 1.5): Stub `table.update_item` with a 500ms delay. Measure time from `dispatch` entry to `call_next(request)` return. On unfixed code, response time ≥ 500ms; on fixed code, response time is independent of the DDB write latency.

6. **Single-worker container / `desiredCount: 1`** (validates 1.6): This is a deployment-level property, not a middleware-level one. Verified by reading `infrastructure/cdk.context.json` and the Dockerfile `CMD`. No runtime test; CDK unit test asserts `DesiredCount: 2` on the production context.

7. **Page-load fan-out amplification** (validates 1.7): Combine (3) + (4) — 8 concurrent requests at a boundary moment. Count blocking DDB calls. Assert ≥ 16 on unfixed code, ≤ 2 on fixed code.

**Expected Counterexamples**:

- Blocked-loop markers do not complete until the stubbed AWS call returns.
- `table.get_item` call count on the stub matches the fan-out, not 1.
- `Set-Cookie` response latency tracks `table.update_item` latency.
- Possible causes confirmed: sync boto3 on event loop, narrow lock scope, aligned constants, inline-awaited slide write.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed middleware produces the expected behavior defined by Property 1.

**Pseudocode:**

```
FOR ALL input WHERE isBugCondition(input) DO
  # (a) event loop non-blocking
  marker_latency := measureConcurrentMarker(dispatch(input))
  ASSERT marker_latency << AWS_call_latency

  # (b) fan-out coalescing
  ddb_get_calls := countGetItemCalls(during_dispatch(input_fanout_n=8))
  ASSERT ddb_get_calls <= 1

  # (c) window staggering
  ASSERT config.slidingRenewalThrottleSeconds
         % config.refreshLeewaySeconds == 0
  ASSERT config.slidingRenewalThrottleSeconds
         > config.refreshLeewaySeconds

  # (d) fire-and-forget slide
  response_latency := measureDispatchTime(input_with_slide)
  ASSERT response_latency independent_of touch_last_seen_latency

  # (e) concurrency slack (deployment assertion)
  ASSERT cdkContextAppApiDesiredCount >= 2
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed middleware produces the same externally observable result as the original middleware.

**Pseudocode:**

```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT dispatch_original(input).response == dispatch_fixed(input).response
  ASSERT dispatch_original(input).set_cookie_headers
         == dispatch_fixed(input).set_cookie_headers
  ASSERT dispatch_original(input).request_state_bff_session
         == dispatch_fixed(input).request_state_bff_session
  ASSERT dispatch_original(input).cleared_cookies
         == dispatch_fixed(input).cleared_cookies
  ASSERT countOf(cognito.initiate_auth across N same-session concurrent requests)
         == 1 per leeway window
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:

- It generates many request shapes across the input domain (cookie present/absent, cookie seal valid/invalid, cache hit/miss, needs_refresh yes/no, rotation yes/no, slide warranted yes/no, absolute cap passed yes/no, `is_enabled()` true/false) and asserts equivalence against a mocked `SessionRepository` + `CognitoRefreshClient`.
- It catches edge cases in the single-flight and fire-and-forget paths that manual unit tests might miss (e.g. an exception inside the single-flight leader; a background slide task racing with the next request).
- It provides strong guarantees that the observable middleware contract is unchanged for the entire `¬C` input domain.

**Test Plan**: First, exercise the unfixed middleware with an expressive `Hypothesis` strategy over request shapes and record observable outputs (response status, `Set-Cookie` headers, `request.state.bff_session`, DDB/Cognito call counts). Then, swap in the fixed middleware and assert equivalence on the same inputs. The strategy must skip any input that satisfies `isBugCondition` — only `¬C` inputs enter the preservation assertion.

**Test Cases**:

1. **Dormant pass-through unchanged** (3.1): With `is_enabled() == False`, every request shape produces identical responses under fixed and unfixed middleware with zero AWS calls.
2. **No-cookie pass-through unchanged** (3.2): Request with no `__Host-bff_session` header, for any method/path, produces identical responses with zero AWS calls.
3. **Unrecoverable cookie clears both cookies** (3.3): Bad-seal, missing-row, expired-row, and terminal-refresh-error inputs produce the same `Set-Cookie` headers with `Max-Age=0` for both `__Host-bff_session` and `__Host-bff_csrf`, same attribute set.
4. **Max-Age re-emit contract** (3.4): For inputs where `_maybe_slide` returns a non-None value, the resulting `Set-Cookie` headers match the original exactly (including attribute set). Fire-and-forget dispatch does not delay or drop the re-emit.
5. **Refresh-storm coalescing preserved** (3.5): For 10 concurrent same-session requests crossing the refresh-leeway window, exactly one `initiate_auth` call is observed on the Cognito stub.
6. **Codec / secret singletons preserved** (3.6, 3.7): Across many requests, `get_default_codec()` returns the same instance, and `resolve_bff_client_secret()` hits Secrets Manager exactly once per process.
7. **CSRF path unchanged** (3.8): Requests that trigger `CSRFMiddleware` produce identical accept/reject decisions with no new I/O.
8. **Absolute lifetime cap preserved** (3.9): Inputs with `created_at + absolute_lifetime_seconds < now` produce `_maybe_slide → None`, no slide write scheduled.
9. **Fail-closed rotation preserved** (3.10): With rotation triggered and `_persist_refresh` forced to exhaust retries, the cache is invalidated and both cookies are cleared.
10. **Cookie decode uniformity** (3.11): All `CookieDecodeError` branches produce identical response shapes and timing profiles on the fixed middleware (no new oracle via single-flight or fire-and-forget).

### Unit Tests

- **Repository offload**: Assert each `SessionRepository.*` method calls `asyncio.to_thread` (monkeypatched) exactly once per call and that the wrapped boto3 call receives the expected arguments. Assert `ClientError` propagation still matches today's behavior.
- **Cognito offload**: Assert `CognitoRefreshClient.refresh` is awaitable, offloads to a threadpool, preserves `CognitoRefreshError`, and returns the same `RefreshResult` shape.
- **Single-flight**: Two concurrent `resolve_once(session_id, factory)` calls share one loader invocation; the entry is removed after completion; an exception in the loader propagates to all waiters; distinct `session_id`s do not share.
- **Fire-and-forget slide**: `_maybe_slide` returns Max-Age before `touch_last_seen` completes; the background task writes to DDB; failure inside the task logs and does not bubble to `dispatch`; the local cache is updated synchronously before the task is scheduled.
- **Config constant**: `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS == 300`; strict multiple of `_DEFAULT_REFRESH_LEEWAY_SECONDS`.

### Property-Based Tests

- **Preservation over `¬C` input domain**: As described in Preservation Checking — generate request shapes, assert fixed ≡ original on response, cookies, `request.state`, and AWS call counts.
- **Fan-out coalescing invariant**: For any N ∈ [2, 32] and any cookie-bearing same-session fan-out, the number of DDB `get_item` calls observed on the stub is ≤ 1 per cache window. Randomize cache warm/cold state, `needs_refresh` outcomes, and concurrent-request arrival ordering.
- **Window-staggering invariant**: For any request timing `t` within one leeway window of a cache TTL boundary, the fixed middleware issues at most one of `{get_item, update_item}` on the critical path — never both.

### Integration Tests

- **End-to-end page-load fan-out**: Drive the app-api container (under `moto` for DDB, a stubbed Cognito client) with a simulated 8-endpoint Angular page load. Measure total wall-clock time and count of DDB/Cognito calls. Assert ≤ 1 `get_item` and ≤ 1 `update_item` across the fan-out, and total latency bounded by the slowest individual handler (not by serialized AWS I/O).
- **Concurrency slack at the deployment boundary**: CDK unit test asserts `DesiredCount: 2` for the production `app-api` service. Integration smoke test asserts that a deliberately slow endpoint (e.g., a route that sleeps 5s) does not stall a concurrent fast endpoint on a parallel request.
- **Refresh-storm under fan-out**: 8 concurrent requests across the refresh-leeway boundary on the same session. Assert exactly 1 Cognito `initiate_auth`, all 8 responses succeed, and `request.state.bff_session` carries the freshly rotated tokens.
