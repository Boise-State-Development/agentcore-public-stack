# Release Notes — v1.0.0-beta.26

**Release Date:** May 13, 2026
**Previous Release:** v1.0.0-beta.25 (May 11, 2026)

---

## Highlights

A small, focused release that lands two operator-facing fixes and one user-facing feature on top of the beta.25 production hardening. The big ones: **multi-sheet XLSX support** in the spreadsheet analysis tool with defensive caps so a pathological workbook can't blow up latency or context, and an **async refactor of the spreadsheet file-lookup path** that closes a regression where concurrent chat load could block the event loop. Also shipping a **user default model preference applied at chat time**, a **green nightly E2E pipeline** after a multi-attempt fix, and **upstream contribution governance** — PRs are now restricted to approved collaborators (GitHub "Collaborators only") and Dependabot version-update PRs are disabled in favor of manual weekly upgrades.

This release has no schema or infrastructure changes. Deploy in any order.

---

## Multi-Sheet XLSX Support in Spreadsheet Analysis

The spreadsheet analysis tool from beta.25 only handled the first sheet of an XLSX file, which silently misled the agent on multi-tabbed workbooks (financial models, fine-tuning datasets, anything from a real BI export). Beta.26 expands the tool to convert every sheet into its own predictable CSV, with sane defaults that protect the latency budget and the model's context window from pathological inputs.

### Backend

- `backend/src/agents/builtin_tools/spreadsheet_analysis/analyze_tool.py` — adds two environment-configurable caps (`MAX_SHEETS_TO_CONVERT`, `MAX_ROWS_PER_SHEET`) so a workbook with thousands of small sheets can't blow out the Code Interpreter sandbox. New helpers:
  - `_sanitize_sheet_name()` produces filesystem-safe deterministic CSV filenames (`stem.sheetname.csv`) so the model's downstream code paths are predictable
  - `_parse_sheet_inventory()` extracts structured sheet metadata from the bootstrap stdout without `eval`/`literal_eval` on untrusted output
  - `_safe_int()` parses bootstrap integers defensively
  - `_format_sheet_note()` generates a markdown footer documenting which sheets converted, which were truncated, and the per-sheet CSV paths — surfacing caps to the model with actionable warnings rather than silently wrong results
- Tool docstring documents the dual contract: single-sheet workbooks keep the legacy `stem.csv` fast path; multi-sheet workbooks get per-sheet CSVs plus a primary alias for the first sheet
- `backend/src/agents/main_agent/core/system_prompt_builder.py` — system-prompt guidance updated so the model handles per-sheet filenames correctly on retries

### Test Coverage

2,800+ lines of new tests across `backend/tests/agents/builtin_tools/spreadsheet_analysis/`:

- `test_analyze_tool_integration.py` (779 lines) — multi-sheet XLSX and CSV workflows end-to-end
- `test_sheet_inventory.py` (307 lines) — parser robustness against malformed bootstrap output
- `test_build_preview_code.py` (127 lines) — filename escaping for quotes and special characters via `repr()` indirection (closes a code-generation injection edge case)
- `test_clean_stderr.py` (202 lines) — `MAX_ERROR_CHARS` budget is now respected strictly, accounting for ellipsis length
- `test_helpers.py`, `test_find_file.py`, `test_list_spreadsheets.py`, `test_strip_first_row.py` — coverage for the smaller utilities

A small robustness fix landed alongside the tests: code generation now stashes the filename as a `_FNAME` variable inside the generated snippet to prevent f-string interpolation conflicts when filenames contain quotes or braces.

---

## Async Spreadsheet File Lookups

The `analyze_spreadsheet` and `list_spreadsheets` tools shipped in beta.25 ran synchronous DynamoDB queries on the event loop (`_find_file`, `_get_kb_files`, `_get_session_files`), and the inference-api `_build_tabular_inventory` chat-route helper used a nested `asyncio.run` + thread pool executor pattern that could block under concurrent chat load. This release converts the entire path to native async: tool entry points are `async def`, every DynamoDB query is offloaded via `asyncio.to_thread`, and the inference-api helper awaits directly. This fixes a regression introduced in #260 where high-concurrency chat traffic could stall the event loop during file lookups — the same class of bug the BFF middleware fix in beta.25 addressed for session resolution.

### Backend

- `backend/src/agents/builtin_tools/spreadsheet_analysis/analyze_tool.py` and `list_spreadsheets_tool.py` — `analyze_spreadsheet`, `list_spreadsheets`, `_find_file`, `_get_kb_files`, `_get_session_files` are all `async def`; DynamoDB calls offload via `asyncio.to_thread`
- `backend/src/apis/inference_api/chat/routes.py` — `_build_tabular_inventory` is now `async` and awaits the file-operation calls directly. Replaces the nested `asyncio.run` + thread pool executor pattern that could deadlock under load

---

## User Default Model Preference

User-saved default model preferences (set in Settings → Chat Preferences) are now actually applied when the chat starts. Previously the persisted `defaultModelId` was ignored and chat fell back to the hardcoded factory default — closes issue #161.

### Backend

- `backend/src/apis/app_api/chat/routes.py` and `backend/src/apis/inference_api/chat/routes.py` — new `_resolve_user_default_model()` helper looks up the persisted `defaultModelId` from user settings. Applied in `chat_agent_stream` and the invocations endpoint when the request does not specify a `model_id`
- RBAC re-checks the resolved default at chat time, so a user whose access to the previously-saved default has been revoked falls back gracefully rather than getting a permission error mid-stream
- A missing user-settings table now surfaces as `503 Service Unavailable` instead of silently dropping the user choice
- `backend/src/apis/app_api/user_settings/routes.py` — defaults endpoint adjustments

### Frontend

- `frontend/ai.client/src/app/session/services/model/model.service.ts` — supports persisted default model resolution
- `frontend/ai.client/src/app/settings/pages/chat-preferences/chat-preferences-settings.page.ts` — Chat Preferences page now wires the default model picker to the persisted setting

### Test Coverage

- `model.service.spec.ts` — 56 lines covering the default-model resolution flow
- `chat-preferences-settings.page.spec.ts` — 101 lines covering the settings UI

---

## Nightly E2E Pipeline Restored

The nightly E2E pipeline had been red since the multi-stack deployment hit a series of cookie/JWT validation issues against the dynamic CloudFront URL. This release lands the fixes that turn the pipeline green:

- CloudFront URL handling for cookie auth in the test environment
- CDK certificate ARN wiring through the nightly job
- Increased agent test time limits (the multi-tool turns were tripping default timeouts)
- Switched the nightly suite from global Bedrock model IDs to US-region IDs to avoid cross-region routing flakes
- Rebased fix branch on `develop` to pick up the release-notes strategy update from #248

---

## Upstream Contribution Governance

A non-code change worth flagging because it changes how external contributors interact with this repository.

- **`CONTRIBUTING.md`** — pull requests are now restricted to approved collaborators only (GitHub "Collaborators only" setting). The repository remains source-available under PolyForm Noncommercial 1.0.0; issues stay open to everyone for bug reports and proposed changes, and a maintainer triages each one. The contributing guide explains the path: open an issue → maintainer triages → maintainer either implements upstream or coordinates next steps with the reporter.
- **`.github/dependabot.yml`** — `open-pull-requests-limit: 0` across all four ecosystems (pip, frontend npm, infrastructure npm, github-actions). Scheduled version-update PRs are off; we handle dependency upgrades manually on a weekly cadence. Dependabot **security updates** are unaffected — when a CVE is published against a dependency, you'll still see a PR.

The full schedules, groups, and labels are retained in the config so flipping the limit back to a positive number restores the previous behavior with a one-line change.

---

## Documentation

- `backend/src/.env.example` — BFF cookie encryption architecture documentation updated to reflect the beta.25 shift from direct KMS cookie encryption to Secrets Manager-mediated approach. Clarifies that the `BFFCookieSigningKey` CMK now encrypts the Secrets Manager secret at rest (not the cookie directly), documents the new `BFF_COOKIE_DATA_KEY_SECRET_ARN` variable, explains the cross-task SHA-256 derivation, and adds the SSM parameter path for locating the secret ARN with an example ARN format

---

## 📦 Dependencies

No dependency upgrades in this release. Dependabot version-update PRs are disabled going forward; the next deps refresh will land as a manually curated batch.

---

## 🏗️ Infrastructure

No infrastructure changes. No new resources, no IAM changes, no SSM parameter changes.

---

## 🔧 CI/CD

- Nightly E2E pipeline fixes (#290) — CloudFront URL handling, CDK certificate ARN, agent test timeouts, US-region Bedrock model IDs

---

## 🚀 Deployment notes

- Deploy in any order. No schema, infrastructure, or IAM changes.
- After deployment, set the `MAX_SHEETS_TO_CONVERT` and `MAX_ROWS_PER_SHEET` env vars on the Inference API task definition if you want non-default caps for the spreadsheet analysis tool. Reasonable defaults are baked into the code; only set these if your workbooks routinely need higher limits.
- **Manual follow-up (not deploy-blocking):** in the GitHub repo settings, flip **Settings → General → Pull Requests → Collaborators only** to actually enforce the contribution policy documented in `CONTRIBUTING.md`. Verify **Settings → Code security → Dependabot security updates** is still enabled — we explicitly want CVE-driven PRs to keep flowing even with version-update PRs disabled.

---

# Release Notes — v1.0.0-beta.25

**Release Date:** May 11, 2026
**Previous Release:** v1.0.0-beta.24 (May 6, 2026)

---

## Highlights

This release is the **production-readiness fix for the BFF Token Handler** shipped in v1.0.0-beta.24. Beta.24 rewrote the SPA's auth surface onto cookie-based sessions but left three production-breaking bugs that only surfaced under real traffic: the `SessionRefreshMiddleware` ran synchronous boto3 on the uvicorn event loop so Angular's ~8-endpoint page-load fan-out produced ~16 serialized blocking AWS calls per user per minute (504s, 80s `/files/quota` tails, 15.6s p-max on a 0.7% CPU task); the `CookieCodec` minted a fresh random AES-256 key per process, so as soon as we raised `desiredCount` for concurrency slack every cookie started failing as `bad seal` on ~50% of requests; and the per-session refresh lock only coalesced in-process, so two tasks could still race `cognito-idp:initiate_auth` with the same refresh token and Cognito's rotation would silently log out the loser. This release lands the **event-loop offload + single-flight resolve**, a **cross-task shared AES key via Secrets Manager**, and a **DDB conditional-write refresh lock** that elects exactly one leader fleet-wide.

Also shipping: **server-rendered PDF page-1 thumbnails** on attachment cards, **rich iMessage-style image mosaics** with a full-screen lightbox and inline markdown preview for `.md` files in user messages, **spreadsheet analysis tools** (`list_spreadsheets`, `analyze_spreadsheet`) that run CSV/XLSX analysis inside the Code Interpreter sandbox, **centralized 401 handling** with proactive session-loss detection on tab refocus, and a **`SKIP_AUTH=true` local-dev bypass** gated by a CORS-origin allowlist and a CI guard workflow. Token accounting was corrected across the board — per-message cost no longer double-counts tool-use turns and the context-% badge reflects current context occupancy rather than Strands' summed-across-calls value.

### Heads-up on beta.24

If you deployed beta.24 to a multi-replica environment, you saw some or all of: 401 storms on `/auth/session`, page-load latency tails in the tens of seconds, and users silently logged out after tab refocus. Beta.25 is the fix. The CookieCodec and refresh-lock changes require redeploying the Infrastructure and App API stacks in order — see **🚀 Deployment notes** at the bottom.

---

## BFF Middleware Event-Loop Blocking & Fan-Out Amplification

The middleware introduced in beta.24 ran three independent classes of work on the uvicorn event loop that weren't safe to run there: synchronous boto3 for DynamoDB + Cognito, an inline-awaited sliding-session write on the response path, and a refresh-coalescing lock that only wrapped the Cognito exchange instead of the full resolve path. Under Angular's ~8-endpoint page-load fan-out with a cold `SessionCache` window, a single cookie-bearing user produced ~16 serialized blocking AWS round-trips on one uvicorn worker running in a single ECS task — every slow call stalled every concurrent request on the same task. The observable symptoms were ALB 504s, `TargetResponseTime` p-max of 15.6s at 0.7% CPU, `/files/quota` outliers reaching ~80s, and endpoint p95s climbing into the hundreds of ms under trivial load. (#264)

### How it works now

`SessionRepository.{get,put,update_tokens,touch_last_seen,delete}` and `CognitoRefreshClient.refresh` now offload every boto3 call via `asyncio.to_thread`, so the event loop keeps scheduling other coroutines for the full AWS round-trip duration. A new per-session single-flight primitive (`apis/shared/sessions_bff/single_flight.py`) wraps the whole `cache.get → repository.get → needs_refresh → (maybe refresh)` block in `SessionRefreshMiddleware._resolve_session` — the first caller per `session_id` runs the loader; N concurrent followers await a shared `asyncio.Future` and consume the leader's result. The existing `get_session_lock(session_id)` around the Cognito exchange is preserved end-to-end as defense in depth. `_maybe_slide` no longer `await`s `touch_last_seen` inline — the DDB write dispatches as a detached `asyncio.Task` and the response returns the fresh `Max-Age` synchronously. The cache/throttle boundary alignment that forced a single request to pay both `get_item` and `update_item` on the cache-miss boundary has been de-aligned: `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` is now a strict multiple of `_DEFAULT_REFRESH_LEEWAY_SECONDS` (300s vs 60s).

### Backend

- `apis/shared/sessions_bff/repository.py` — every boto3 call now wrapped in a nested sync helper invoked via `await asyncio.to_thread(helper, ...)`; method signatures, return types, and exception branches unchanged
- `apis/shared/sessions_bff/refresh.py` — `refresh` is now `async def`, calling `await asyncio.to_thread(self._refresh_sync, ...)`; `CognitoRefreshError` contract and `RefreshResult` shape preserved verbatim
- `apis/shared/sessions_bff/single_flight.py` — new module. `async def resolve_once(session_id, loader_coro_factory) -> tuple[Optional[SessionRecord], bool]`. Leader registers an `asyncio.Future` under a thread-lock-guarded `dict`, runs the loader, sets the result/exception on the Future, removes the registry entry in a `finally` block. Followers `await` the existing Future. Distinct `session_id`s never share a Future
- `apis/shared/middleware/session_refresh.py` — `_resolve_session` wraps the cache/repo/refresh block in `resolve_once(session_id, _loader)`. `_maybe_slide` updates the local cache synchronously and dispatches `touch_last_seen` via `asyncio.create_task`, keeping the task on `self._slide_tasks` with an `add_done_callback(self._slide_tasks.discard)` — Python's asyncio docs explicitly warn that unreferenced tasks can be GC'd mid-flight, and our initial fix landed this footgun (caught by CI on Python 3.12)
- `apis/shared/sessions_bff/config.py` — `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` raised 60s → 300s. Strict multiple of the 60s leeway guarantees cache-miss and slide-throttle boundaries never coincide

### Infrastructure

- `infrastructure/cdk.context.json` — `appApi.desiredCount` raised 1 → 2 for concurrency slack. A single blocked event loop on one task can no longer halt all ingress

### Test Coverage

~900 lines of new property-based tests. `test_session_refresh_bug_condition.py` encodes each of the seven sub-conditions as a hypothesis property that fails on unfixed code and passes on fixed code (Property 1 / Expected Behavior from the bugfix spec). `test_session_refresh_preservation.py` locks in the 11 preservation invariants that must stay unchanged for non-buggy inputs — dormant pass-through, no-cookie pass-through, unrecoverable-cookie clearing, `Max-Age` re-emit contract, refresh-storm coalescing, codec + client-secret singletons, CSRF decision unchanged, absolute-lifetime cap, fail-closed rotation, uniform `CookieDecodeError` handling. `test_single_flight.py` covers the primitive itself: concurrent callers share one loader invocation, exceptions propagate to every waiter, registry entries clean up after failure, distinct sessions are independent.

---

## BFF Cross-Task Cookie & Refresh Correctness

The `desiredCount: 1 → 2` bump in the event-loop fix immediately exposed two latent defects in beta.24's BFF design that were hidden when only one task existed. Both had to be fixed before the deployment was actually safe to run with more than one replica. (#273, #274, #275)

### Shared AES-256 data key via Secrets Manager

`CookieCodec` in beta.24 called `kms:GenerateDataKey` on first use per process and cached the resulting plaintext AES-256 key in memory. The code's own docstring predicted what would happen with more than one task: _"two codecs in one process can never decrypt each other's output."_ And that's what happened — Task A sealed a cookie with Key-A, the ALB routed the follow-up to Task B which had its own Key-B, `unseal` hit `InvalidTag` → `CookieDecodeError` → `Discarding unrecoverable BFF cookie (bad seal)` → 401. CloudWatch confirmed: three app-api streams each independently logged _"BFF cookie codec initialized (KMS data key fetched)"_ and every subsequent `/auth/session` returned 401.

The fix moves the data key out of per-process state and into a single Secrets Manager secret, encrypted at rest by the existing `BFFCookieSigningKey` CMK:

- CDK creates `BFFCookieDataKeySecret` with `generateSecretString` (44-char alphanumeric, ~261 bits of entropy). On every deploy the secret already exists so the value is stable — cookies survive redeploys
- `CookieCodec._ensure_cipher` reads the secret string and applies SHA-256 to derive the 32-byte AES-256 key. Single-shot SHA-256 of a ≥256-bit-entropy random input is a sound KDF for AES-256 usage
- Every app-api task decrypts the same secret and derives the same key → all codecs round-trip each other's seals. The `kms:GenerateDataKey` permission dropped from the runtime task role (least privilege); `kms:Decrypt` stays because Secrets Manager invokes it on the caller's behalf when reading a CMK-encrypted secret

A previous attempt at this bootstrap (#273's initial chained `AwsCustomResource` flow with `kms:GenerateDataKey → secretsmanager:PutSecretValue`) failed stack create with `Response object is too long`. Root cause: the `AwsCustomResource` framework Lambda JSON-stringifies the AWS-SDK response before applying `outputPaths`, and KMS returns `CiphertextBlob` as a Uint8Array that serializes as `{"0":233,"1":18,...}` — ~1.5 KB for a 200-byte ciphertext, past CloudFormation's 4 KB response-object limit. The Secrets-Manager-native `generateSecretString` path in #274 removes the chained custom resources entirely (-153 lines net), no per-cold-start `kms:Decrypt` call, simpler runtime IAM surface.

### Cross-task refresh lock via DDB conditional-write

The in-process single-flight and the existing `get_session_lock` only coalesce same-session callers within one Python process. Once the cookie-codec fix lands and both tasks can share cookies again, under `desiredCount: 2` two tasks each receive a same-session request crossing the refresh-leeway window and each call `cognito-idp:initiate_auth` with the same refresh token. Cognito rotates on the winning call; the loser receives `NotAuthorizedException`, the loser's middleware clears the cookie, and the user is silently logged out.

- `SessionRepository.try_acquire_refresh_lock(session_id, owner, lock_ttl_seconds)` — conditional `UpdateItem` that succeeds iff `attribute_not_exists(refresh_lock_until) OR refresh_lock_until < :now` AND `attribute_exists(PK)` (no phantom rows for sessions that don't exist). Loser returns `False`
- `SessionRepository.update_tokens` gains `expected_lock_owner=...` — when supplied, the write conditionally requires `refresh_lock_owner = :owner` (strict, not "owner-or-absent") and atomically `REMOVE`s the lock attrs in the same write. The stale-leader-stomp case (Task A's lock TTLs, Task B refreshes, Task A returns with older tokens) now surfaces as `ConditionalCheckFailedException` so the caller can re-read and adopt the peer's tokens
- `SessionRepository.release_refresh_lock(session_id, owner)` — best-effort cleanup for the leader-failed path so a peer doesn't have to wait the full TTL before retrying
- `SessionRefreshMiddleware._resolve_session._loader` — two-tier coalescing: (1) existing `get_session_lock` collapses N in-process same-session callers to one contender; (2) `try_acquire_refresh_lock` elects exactly one leader fleet-wide. Followers poll the row via `_wait_for_peer_refresh` and adopt the leader's tokens (rotation detected by refresh-token mismatch; non-rotation by access-token mismatch + future-dated `exp`). Absolute-lifetime guard added ahead of the lock acquisition — if `now > created_at + absolute_lifetime_seconds`, clear the cookie instead of burning a Cognito refresh on a row that's about to TTL-evict

### Test Coverage

Cross-task integration tests (`test_session_refresh_cross_task.py`, 480 lines) run two `SessionRefreshMiddleware` instances against one moto DDB table and exercise leader/follower paths, follower-polling-then-adopting, lock TTL recovery after a dead leader, follower-fall-back-terminal when the leader is stuck, and the headline invariant: two tasks racing in parallel call Cognito at most once. Eight new repository tests lock the lock primitive shape, plus targeted tests for the strict-owner release condition and the phantom-row-prevention guard on acquire.

### Infrastructure

- New `BFFCookieDataKeySecret` (Secrets Manager), encrypted with `BFFCookieSigningKey`. SSM parameter `/${projectPrefix}/auth/bff-cookie-data-key-secret-arn` publishes the ARN for app-api
- App-api task role: added `secretsmanager:GetSecretValue` on the new secret; kept `kms:Decrypt` (needed by Secrets Manager to read the CMK-encrypted secret); removed `kms:GenerateDataKey` and `kms:DescribeKey`
- No IAM change required for the DDB refresh lock — app-api task role already had `dynamodb:UpdateItem` on `BFFSessionsTable`

### Breaking changes

- None user-facing. The new env var and SSM parameter are additive; existing deployments redeploy Infrastructure first, then App API, to pick up the shared secret

---

## Token Accounting Correctness

Two related bugs were inflating cost and context-% reporting on tool-use turns. (#270)

### Per-message cost double-count

Strands emits per-LLM-call metadata (each call's tokens) AND a final `AgentResultEvent` whose `EventLoopMetrics.accumulated_usage` is summed across every call in the turn. Both were emitted as `metadata` events and routed into `per_message_metadata[current_assistant_message_index]["usage"]` via `.update()`. Because the `AgentResult` event arrives after every `message_stop`, the index still pointed at the last assistant message — so cumulative tokens overwrote that message's per-call values, double-counting earlier messages' input tokens when each entry was priced and summed.

Fix: route the result-extracted cumulative on the existing `metadata_summary` (turn-summary) track instead of `metadata`. The `stream_processor` main loop consumes both event types into `accumulated_metadata` so the final summary still carries true totals.

### Context-% inflation within a tool turn

Bedrock reports each per-LLM-call `inputTokens` as the FULL context size sent on that call. For a 2-call tool turn (`call_1.input=1000`, `call_2.input=2500`), Strands' `accumulated_usage` reports 3500 — but the actual current context occupancy is 2500. The final SSE `usage` field driving the context-% badge and compaction trigger was inheriting Strands' summed value.

Fix: `stream_coordinator` no longer accumulates `metadata_summary` into `accumulated_metadata`. Per-call `metadata` events last-write-wins via `.update()`, so `accumulated_metadata.usage` equals the most recent call's full input = current context. Added a `CAUTION` comment noting `AgentResult.context_size` / `EventLoopMetrics.latest_context_size` return only `inputTokens` (excluding `cacheRead` / `cacheWrite`) — under prompt caching they under-report by 99%+, so we deliberately sum all three buckets. `TTFT` placeholder of 0 changed to `null` (a real time-to-first-token can never be 0ms and aggregations need to distinguish absence from a real zero); `LatencyMetrics.time_to_first_token` is now `Optional[int]` in both the shared and app-api models.

### Test Coverage

`test_per_message_cost_attribution.py` pins the `metadata` vs `metadata_summary` contract, the main-loop accumulator's both-tracks consumption, and the `stream_coordinator` current-context semantics (two parametrized cases plus all-three-buckets-summed for cache-read/write). Direct unit coverage for `CostCalculator` arrived in `test_calculator.py` (26 cases: per-bucket pricing, cache scenarios against Sonnet 4.5 rates, defensive missing-key / None handling, `calculate_cache_savings`, `validate_pricing` / `validate_usage`).

---

## Auth UX & Local-Dev Bypass

### Centralized 401 handling + proactive session detection

Beta.24 only redirected on 401 from the SessionService bootstrap path — a session that expired mid-session left the user stranded with a generic toast (CRUD endpoints) or no feedback (SSE chat stream). Every 401 now flows through `SessionService.handleUnauthorized()`, which dedupes concurrent calls and queues a single navigation to `/auth/login` with a preserved `returnUrl`. Session loss is surfaced proactively rather than waiting for the next HTTP call to fail: (#277)

- **Cookie-presence fast-path** in bootstrap and recheck. The JS-readable `__Host-bff_csrf` cookie is set and cleared alongside `__Host-bff_session` with matching `Max-Age`, so if the CSRF cookie is gone the session cookie is gone too — skip the `/auth/session` round-trip and bounce straight to login
- **Visibility re-probe** in the app shell. On tab refocus, `recheck()` runs the cookie check and falls back to `/auth/session`, so a session that expired while the tab was backgrounded is caught immediately rather than on the next user action

### `SKIP_AUTH=true` local-dev bypass

A single-env-var bypass for unattended local dev (and Claude Code agents) that can't round-trip through an external IdP. (#272)

- Returns a fake admin `User` from the three auth dependencies in `apis.shared.auth.dependencies`; CSRF middleware, RBAC, and profile cache flow naturally because no `bff_session` is resolved
- **Allowlist startup guard** in `app_api/main.lifespan` — app refuses to boot when `SKIP_AUTH=true` is paired with any non-localhost entry in `CORS_ORIGINS` (or an empty `CORS_ORIGINS`). Fails closed for deploy targets we haven't anticipated rather than blocklisting known cloud env vars
- **CI guard workflow** (`.github/workflows/skip-auth-guard.yml`) — greps CDK source, workflow files, and Dockerfiles for `SKIP_AUTH=true` / `SKIP_AUTH: true` patterns and fails the build if any leak into deployed config
- Inference-api is intentionally not bypassed — all SPA traffic flows through app-api per the BFF pattern, so one bypass is sufficient
- Optional tuning: `SKIP_AUTH_ROLES`, `SKIP_AUTH_USER_ID`, `SKIP_AUTH_EMAIL` override the default fake user

### Lava-lamp backdrop dark-mode fix

The dark-mode CSS for the auth pages' lava-lamp backdrop and frosted-glass card never applied on cold load: hand-written `html.dark .X` selectors don't match under Angular's emulated view encapsulation, and `ThemeService` (`providedIn:'root'`) was never injected by anything in the pre-auth tree. Switched the auth-page CSS to `:host-context(html.dark) .X` (the pattern already used component-scoped elsewhere) and forced `ThemeService` to construct at bootstrap via `provideAppInitializer`, so the persisted/system theme is applied to `<html>` before any route renders, including `/auth/login` and `/auth/first-boot` on cold load. (#271)

---

## Attachments: PDF Thumbnails, Rich Previews, Markdown Modal

### Server-rendered PDF page-1 thumbnails

Real first-page thumbnails for PDF attachments instead of the skeleton mockup. Page rasterization runs in app-api via `pypdfium2` (Apache 2.0 / BSD, bundled PDFium binary, no system `poppler`/`ghostscript`). (#263)

- New `ThumbnailRenderer` with a MIME-type dispatcher; PDF only today. Class docstring documents the recommended out-of-process design for `.docx` / `.xlsx` so the dispatcher stays small
- `GET /files/{upload_id}/thumbnail` — lazy: HEAD-checks for a cached `_thumb.png` sibling next to the original, renders + stores on miss, returns a short-lived presigned GET URL. 415 for unsupported MIME types, 422 for unreadable / corrupt PDFs. Render runs in `loop.run_in_executor` so request workers aren't blocked
- Single-file and session-cascade deletes also remove the thumbnail sibling
- `FileUploadService.getThumbnail()` returns a typed result so callers switch on `ready` / `unsupported` / `unavailable` without parsing HTTP errors. Badge fetches on mount for PDFs and renders as `object-cover`, suppressing the bottom fade. Silent fall-back to the skeleton on any error

### Rich previews in user messages

The dense badge is replaced with a richer attachment renderer in user message history. (#254)

- **Images** render as an iMessage-style mosaic: 1-bubble, 2-col, 1+2 split, 2×2 grid, 5+ with `+N` overlay. Opens in a full-screen lightbox with arrow-key navigation
- **Non-image files** render as a document-style card: tinted header strip with type chip, white "page" body with a folded corner, filename + size footer. Text-based files (txt, md, csv, html) show a real content excerpt; binary types (pdf, docx, xls/xlsx) get skeleton lines
- `GET /files/{upload_id}/preview-url` — short-lived presigned GET URL scoped to the file owner, used for inline images and the lightbox
- `GET /files/{upload_id}/text-snippet` — first 2KB of a text-based file decoded as UTF-8 for the document card content peek

### Inline markdown preview for `.md` files

Parsed markdown renders in the attachment card excerpt instead of raw text; clicking a `.md` card opens a full-screen modal viewer rather than opening the raw source in a new tab. Reuses `ngx-markdown` (already wired up for assistant messages) and the existing presigned preview-url flow. (#262)

---

## Spreadsheet Analysis Tools

New spreadsheet analysis capability for CSV/XLSX files. (#f88ce7ec, #0ab90bb1)

- `list_spreadsheets` — enumerates CSV/Excel files from knowledge bases and chat attachments; includes file size and MIME type metadata
- `analyze_spreadsheet` — downloads files from S3, executes Python analysis via Code Interpreter, returns results. Intelligent schema detection with skiprows probing handles report-style exports with metadata rows. Stderr is cleaned to filter pandas/numpy internal frames and show only user-relevant errors. Output truncated at 10K chars, errors at 600 chars, to prevent context-window overflow
- Tools injected per-request into `ToolRegistry` via `extra_tools`; chat routes (app-api and inference-api) pass conversation context to the factories
- Targeted error hints for XLSX→CSV filename mismatches in the sandbox environment; tolerant filename matching for CSV↔XLSX aliasing to prevent retry loops; schema footer preservation on errors for better retry context
- File metadata models and utilities for consistent attachment handling; stream processor error handling improved for Code Interpreter responses

---

## 📦 Dependencies

| Package | From | To |
|---|---|---|
| strands-agents (backend) | 1.37.0 | 1.39.0 |
| strands-agents-tools (backend) | 0.5.1 | 0.5.2 |
| pypdfium2 (backend, new) | — | latest |

`CacheConfig(strategy="auto")` remains intentionally deferred on `BedrockModel`. The strands v1.39.0 bump includes the SDK-side fix (strands PR #1438 — `cachePoint` blocks alongside non-PDF document attachments), so the technical barrier is gone — but the user-visible cost/badge impact warrants a separate scoped rollout. (#265)

---

## 🏗️ Infrastructure

- **New**: `BFFCookieDataKeySecret` (Secrets Manager), encrypted at rest with the existing `BFFCookieSigningKey` CMK. SSM parameter `/${projectPrefix}/auth/bff-cookie-data-key-secret-arn`
- **Changed**: `appApi.desiredCount` raised 1 → 2
- **IAM delta on app-api task role**: added `secretsmanager:GetSecretValue` on `BFFCookieDataKeySecret`; removed `kms:GenerateDataKey` and `kms:DescribeKey` on `BFFCookieSigningKey`; kept `kms:Decrypt` (Secrets Manager invokes it on the caller's behalf when reading a CMK-encrypted secret)
- **No new tables**. The cross-task refresh lock reuses `BFFSessionsTable` via conditional `UpdateItem`

---

## 🔧 CI/CD

- **New workflow**: `.github/workflows/skip-auth-guard.yml` — greps CDK source, workflow files, and Dockerfiles for `SKIP_AUTH=true` / `SKIP_AUTH: true` patterns and fails the build if any leak into deployed config. Uses SHA-pinned `actions/checkout` and `ubuntu-24.04` per existing supply-chain conventions in `tests/supply_chain/`

---

## 🚀 Deployment notes

Deploy Infrastructure first, then App API, in that order.

1. **Infrastructure stack** creates `BFFCookieDataKeySecret` and publishes its ARN to SSM. The secret value is generated by Secrets Manager on create and stays stable across subsequent deploys — cookies survive redeploys
2. **App API stack** picks up `BFF_COOKIE_DATA_KEY_SECRET_ARN` on the next task rotation; existing tasks keep the old per-process data key until they drain. Both states coexist cleanly — new tasks seal under the shared key; old tasks still seal under their own; unsealing on a task that holds a different key fails the same way it does today and the SPA bounces to login. End state (all tasks rotated): cookies round-trip cleanly across the fleet
3. **`desiredCount: 2` takes effect** on the App API stack's next deploy. CloudFormation scales up without draining traffic; the fix makes multi-replica safe

No manual cleanup required if you were running on beta.24 — the migration is forward-only. If you want zero-drift on the user population, invalidate active sessions once post-deploy: `aws dynamodb scan --table-name ${BFFSessionsTable} --select COUNT` then a bulk delete, or just let the 30-day absolute-lifetime cap roll them off naturally.

---



---

## BFF Token Handler — Cookie-Based Auth

The SPA's entire auth surface has been rewritten. Bearer tokens in `localStorage` are out; an opaque session id in a `__Host-bff_session` httpOnly cookie is in. The public PKCE Cognito client is decommissioned in favor of a confidential BFF client whose secret never leaves the server. Chat streams and voice WebSockets now transit same-origin `/api/*` through CloudFront, with app-api proxying to inference-api server-side. This closes the window where an XSS could exfiltrate a long-lived Cognito access token, removes the CORS preflight from every chat turn, and sets the foundation for the voice re-enablement below.

### How authentication works now

A successful login goes: SPA → `GET /auth/login` → Cognito Hosted UI (with PKCE) → `GET /auth/callback` on app-api. The callback exchanges the code server-side using the confidential client secret, writes the Cognito access/refresh/ID tokens to `BFFSessionsTable` keyed by an opaque session id, and seals that id into an AES-GCM cookie whose data key is wrapped by KMS. The browser never sees a JWT. Subsequent requests carry only the cookie; `SessionRefreshMiddleware` unseals it, looks up the session row, silently refreshes the Cognito token when it's near expiry, and forwards the request. Unsafe methods require a double-submit CSRF header matching the `__Host-bff_csrf` cookie.

### What shipped

**Backend (`apis/shared/sessions_bff/`).** `CookieCodec` (AES-GCM with version-byte associated data, promoted to a process-wide singleton so the `/auth/callback` seal and middleware unseal share the same KMS-derived key), `BFFSessionRepository` with conditional TTL writes, `SessionRefreshMiddleware` and `CSRFMiddleware` on app-api, per-session `asyncio.Lock` so multi-tab refresh storms drive exactly one Cognito exchange, and a Cognito refresh-token client that retries rotation writes three times before failing closed (an old refresh token dies the instant Cognito rotates it, so a silently-failed write would log users out on the next request).

**BFF auth routes.**

- `GET /auth/login` — Cognito authorize with PKCE, optional `identity_provider` for federated one-click SSO, optional `return_to` for deep-link preservation. `_sanitized_return_to` rejects all C0 control bytes (U+0000..U+001F), not just CR/LF, so browser URL-parser strip tricks like `/\t/evil.com` can't pivot through the `//` check.
- `GET /auth/callback` — server-side code exchange, cookie seal, upsert of the Users row directly from ID-token claims (`email`, `name`, `picture`, `custom:roles` / `cognito:groups`); previously the per-request sync ran off the access token, which carries no email, so first-login users had `email=None` and the Cognito provider-group string in `roles` instead of the IdP-mapped values.
- `GET /auth/session` — returns the session payload the SPA uses to bootstrap.
- `POST /auth/logout` — clears cookies, invalidates the DDB row, returns `{post_logout_url}` pointing at `{cognito_domain}/logout` so the browser bounces through Cognito Hosted UI to clear the upstream session. Without this, Cognito silently re-issued a code on the next login without a credential prompt.

**Sliding session lifetime.** The cookie's `Max-Age` and the DDB row's TTL bump on every successful resolution, capped at `created_at + BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS` (default 30 d) and throttled by `BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS`. Without this, active users were getting logged out after 1 hour even though their refresh token was valid for 30 days.

**Chat SSE proxy.** `POST /chat/stream` on app-api is the cookie-authenticated proxy to `{INFERENCE_API_URL}/invocations`. It owns its `httpx.AsyncClient` lifecycle and closes it in the streaming generator's `finally` block — using `async with` would drain the upstream during `__aexit__` and buffer the entire stream before headers flush. Forwards the SPA's `OAuth2CallbackUrl` header so `AgentCoreContextMiddleware` can scope tool-side OAuth consent landing URLs to the SPA origin. The AgentCore Runtime data-plane URL is built by `_build_upstream_url()`, which percent-encodes the ARN as a single path segment and appends `?qualifier=DEFAULT` — without this the ARN's literal `/` split the path and AWS returned 404. Sets `X-Accel-Buffering: no` and `Cache-Control: no-cache` so late SSE events (notably `oauth_required` after `message_stop`) reach the browser. The same lifecycle fix was mirrored onto the API-key-authenticated `/chat/api-converse` proxy.

**Frontend (`SessionService`).** Bootstraps from `GET {appApiUrl}/auth/session` in a chained `APP_INITIALIZER` (migrated to Angular 19+ `provideAppInitializer`). On 401, navigates to the SPA's `/auth/login` page with `returnUrl` — not Cognito Hosted UI directly — so the user can pick a provider. The bootstrap promise hangs on the 401 path so `APP_INITIALIZER` stays pending until the browser tears the page down (previously the router could render `/` in the brief window before navigation landed). A new `csrfInterceptor` mirrors the CSRF header onto unsafe-method requests; a new `withCredentialsInterceptor` flips `withCredentials: true` on every `HttpClient` call to `appApiUrl` (local dev runs cross-origin; production is same-origin via CloudFront so the flag is a no-op, but without it cross-origin dev 401'd on every call after login). `ChatHttpService` and `PreviewChatService` target `${appApiUrl}/chat/stream` with `credentials: 'include'` instead of hitting inference-api directly.

**Legacy AuthService retired.** `auth.service.ts`, `auth.interceptor.ts`, the SPA's `/auth/callback` page + `callback.service.ts`, and their specs are deleted. `UserService.currentUser` is derived from `SessionService.user()`. `authGuard` and `adminGuard` gate on `SessionService.isAuthenticated()`. The SPA `/auth/callback` route is gone — the BFF callback at `${appApiUrl}/auth/callback` is the only OAuth landing.

**Infrastructure.** `BFFSessionsTable` (DynamoDB, TTL attribute), `BFFCookieSigningKey` (KMS), `CognitoBFFAppClient` (confidential, secret in Secrets Manager). CloudFront `/api/*` behavior on the frontend distribution forwards to the app-api ALB with a viewer-request Function that strips the `/api` prefix. Caching disabled, all-viewer-except-host-header policy, no compression (SSE must not be re-gzipped), `readTimeout` capped at CloudFront's 60 s default max. SPA fallback moved off distribution-wide `errorResponses` (which was rewriting `/api/*` 4xx into 200 + `index.html`, choking `HttpClient` JSON parsing) onto a viewer-request Function scoped to the S3 behavior. `CognitoConfig.supportedIdentityProviders` (env `CDK_COGNITO_SUPPORTED_IDPS`) wires federated IdPs onto the BFF client; previously only the now-deleted public client had them.

**Public PKCE client decommissioned.** The SPA-public `appClient` is gone, along with SSM parameters `/auth/cognito/app-client-id` and `/oauth/callback-url`. `InferenceApiStack`'s runtime authorizer repoints to `/auth/cognito/bff-app-client-id`. `AppApiStack`'s `COGNITO_APP_CLIENT_ID` also repoints to the BFF client, which keeps `/chat/agent-stream` Bearer validation alive for API-key and scripted callers.

**`/config.json` retired.** `appApiUrl` is baked into the bundle via Angular `fileReplacements` (dev → `http://localhost:8000`, prod → `/api`). `version` is generated from the monorepo root `VERSION` file by a `scripts/gen-version.js` prebuild hook. `cognitoDomainUrl` is fetched on demand from a new `GET /admin/auth-providers/cognito-redirect-uri` admin endpoint. `ConfigService` collapses to a thin signal accessor over `environment.appApiUrl`; `APP_INITIALIZER` drops the chained `loadConfig` step.

### Breaking changes

- **`Authorization: Bearer` is no longer accepted on SPA-facing routes.** Cookie auth is required. External callers must migrate to the BFF session flow or hit `/chat/agent-stream` (Bearer-only) instead.
- **`POST /chat/stream` is now the cookie-authenticated proxy.** The legacy in-process agent loop moved to `POST /chat/agent-stream` for API-key and scripted callers.
- **SPA `/auth/callback` route removed.** Third-party tools that deep-linked there must use `${appApiUrl}/auth/callback`.
- **SSM parameters deleted:** `/auth/cognito/app-client-id` and `/oauth/callback-url`. Consumers must migrate to `/auth/cognito/bff-app-client-id` and register a per-system callback URL.

---

## Voice Mode via WebSocket-Ticket Proxy

Voice returns on top of the new cookie flow. The SPA no longer holds a Cognito access token, so it can't authenticate the WebSocket upgrade against the AgentCore Runtime's `customJwtAuthorizer` directly. Instead the SPA mints a single-use HMAC ticket, opens a same-origin WS to `/api/voice/stream`, and app-api opens the upstream WS using the BFF-stored Cognito token (#211, #233).

### How it works

- `POST /voice/ticket` (cookie + CSRF auth) issues a 60-second ticket bound to `{user_sub, session_id, jti, exp}`
- WebSocket `/voice/stream` gates on Origin allowlist, cookie unseal, ticket verify + replay (via `VoiceTicketReplayTable`, jti partition key, TTL attribute), and ticket↔session `user_id` binding before relaying
- The aiohttp WS relay rewrites `auth_token` and `user_id` on every text-type `config` frame — not a one-shot flag, which would have let a SPA that sent any non-config frame first consume the injection slot and forge identity on subsequent frames
- New infrastructure: `VoiceTicketReplayTable` and `VoiceTicketSigningSecret` (Secrets Manager), plus IAM grants and `VOICE_TICKET_*` env vars on app-api; inference-api unchanged

### Shared primitive

`apis/shared/voice_ticket/` packages the HMAC-SHA256 codec, the DynamoDB conditional-put replay store, and a service facade that enforces verify-then-consume atomically.

### Frontend

- `VoiceTicketService` makes the REST hop; `VoiceChatService` opens WS at `${appApiUrl}/voice/stream?ticket=…` and sends a `config` frame without `auth_token` (the proxy injects it upstream)

Covered by 30 backend tests (codec, replay, service, URL builder, config injection, route auth gates) and 2 frontend tests.

---

## Per-Conversation Cost + Context-Window Badge

A compact badge above the full-page composer shows the running USD cost of the current conversation and a color-graded SVG ring filled by the most recent turn's context-window usage (#223).

### Backend — write-time aggregation

After each cost-record `put_item`, an atomic `ADD totalCost` / `SET lastContextTokens, contextWindow` bumps the session row. Metadata GET becomes a single `GetItem` instead of a per-turn GSI scan. Legacy sessions lazily backfill on first read (sum the C# records once, write totals back) — no migration script needed. `StreamCoordinator` looks up `max_input_tokens` for the current model and surfaces it both on the SSE `metadata` event (live badge) and on stored `MessageMetadata` (persistence).

### Frontend

- `ChatStateService` gains `costDollars`, `contextTokens`, `contextWindowSize`, and computed `contextPct` signals
- Seeds from session metadata on route change; clears stale state before new metadata loads; increments per-turn from the SSE `metadata` event
- SVG ring animates in from empty on first render and smoothly between turns; color steps through emerald → blue → amber → red as fill increases; tooltip surfaces underlying token counts and notes that the total includes system prompt + tools
- Theme-aware fade gradient above the composer so messages scrolling under the fixed footer fade out instead of cutting against a hard edge

### Correctness fixes folded into the feature

- Multi-step tool-loop turns emit multiple metadata events per message (intermediate plus cumulative); the initial implementation priced the last event and undercounted. Now walks per-message metadata, prices each independently, and sums — matching the per-message C# records persisted server-side.
- `inputTokens` from Bedrock is the uncached portion only. The cached prefix and freshly-cached content live in `cacheReadInputTokens` / `cacheWriteInputTokens`. Summing all three buckets in three places (live frontend update, `_bump_session_aggregates`, legacy-session backfill) gives true context-window occupancy; gating the badge update on `data.contextWindow` being present (only attached to the end-of-turn synthesized event) stops per-call intermediates from overwriting the badge mid-turn.

---

## Context Compaction Events with Refresh-Survival

When the backend rolls older turns into a summary to keep input under the token threshold, users now see a subtle "Earlier messages summarized" indicator at the bottom of the conversation with a tooltip showing the cumulative turn count — explaining the sudden context-window drops that show up on the cost badge (#243).

### Backend

- New `compaction` SSE event in `StreamCoordinator`, emitted after the final `metadata` event so the cost badge updates before the indicator changes (payload: `previousCheckpoint`, `newCheckpoint`, `summarizedTurns`, `inputTokens`)
- `TurnBasedSessionManager.update_after_turn` returns `CompactionResult` on checkpoint advance and accepts `current_messages` so the cutoff cache stays correct when AgentCoreMemory loads via hooks
- `CompactionState` carries a cumulative `totalSummarizedTurns` counter persisted alongside the nested compaction map; lifted to a top-level field on the session-metadata GET so the frontend can rehydrate after refresh without knowing the internal state shape
- Lazy-load fix: on the AgentCoreMemory existing-session path, `agent.messages` is empty during `initialize()`, so `_apply_compaction()` skipped `_load_compaction_state`. The first sub-threshold `update_after_turn` then saved default zeros over the persisted counter. Tracked via `_compaction_state_loaded` and lazy-loaded on first `update_after_turn` if not.

### Frontend

- `CompactionSummaryService` holds the running total as a signal; `recordLive` for SSE events, `seedFromHydration` for session-load replay. A `wasHydrated` flag suppresses the one-shot fade-in animation on reload while still firing it for live events.
- End-of-conversation indicator replaces the original per-message inline divider (which caused jarring layout shifts)
- `session.page` seeds from `currentSession.totalSummarizedTurns` and resets the service on session change so totals don't bleed across sessions

---

## Per-Model Inference Parameters with Extended Thinking

Replaces the global `temperature` / `max_tokens` knobs with a per-model `supportedParams` map keyed by canonical name (`temperature`, `top_p`, `top_k`, `max_tokens`, `thinking`, `reasoning_effort`, etc.). Admins author which params apply to each model, the runtime translates canonical names into provider-native shapes (Bedrock / OpenAI / Gemini), and users can override per-request from a new Settings → Advanced panel (#203).

### Extended thinking on Anthropic Bedrock

- Stored as an int budget per model; runtime wraps it into the `{type, budget_tokens}` Anthropic request shape under `additional_request_fields` (the field Strands' `BedrockConfig` actually forwards — the previously-attempted `additional_model_request_fields` was dropped)
- Suppresses `temperature` / `top_p` / `top_k` while thinking is on (Anthropic constraint)
- Validated up front: budget ≥ 1024 and < `max_tokens`, with inline errors on the admin form, an "unsatisfiable" disabled state on the user panel when `max_tokens` drops below the floor, and a final cross-param safety drop in the merge step so direct API callers never ship a Bedrock-rejecting request

### Persistence fix for thinking + tool use

The persistence-side `_filter_empty_text` in `TurnBasedSessionManager` was dropping `reasoningContent` blocks. Anthropic requires the prior thinking block (with its signature) to be replayed verbatim while a tool-use cycle is open; losing it triggers `messages.X.content.Y.thinking.signature: Field required` on subsequent Bedrock calls. Replaced the narrow allowlist with the full set of Bedrock Converse content block keys mirrored from Strands' `BedrockModel._format_request_message_content`, with a warning when an unrecognized block is dropped.

### Safety hardening

- `_merge_inference_params` gates request-side passthrough against a `KNOWN_CANONICAL_PARAMS` allow-list (union of all provider mapping keys) so future canonical keys a future provider mapping might forward can't bypass per-model bounds
- `lastTemperature` on `SessionPreferences` and the dead `isReasoningModel` field on `ManagedModel` are removed

---

## Login Page Redesign

A translucent backdrop-blur card floats over a layered primary-color background with soft drifting blobs, a masked grid overlay, and a subtle inset highlight (#246). Light/dark themes both supported; animation respects `prefers-reduced-motion`. The Cognito button now uses the app's primary color instead of a generic blue.

---

## Backend Architecture Cleanup

Completes the multi-release decoupling of app-api from inference-api and the agent layer (#200). Moves from `apis.app_api` into `apis.shared`:

- `costs/` — calculator, pricing_config, models, aggregator
- `auth/api_keys/` — models, service, repository
- `tools/` — models, repository, freshness
- `storage/` — metadata_storage, dynamodb_storage

New AST-based architectural boundary tests (`tests/architecture/test_import_boundaries.py`) enforce:

- `inference_api` never imports from `app_api`
- `agents/` never imports from `app_api` or `inference_api`
- `apis.shared` never imports from `app_api` or `inference_api`
- `app_api` never imports from `inference_api`

Updates `CLAUDE.MD` and steering docs with the import boundary rule. Closes #120.

---

## RAG Ingestion Improvements

Tabular data ingestion rewrite and embeddings scaling fix for the RAG pipeline.

### XLSX chunker

A new `xlsx_chunker.py` converts Excel sheets to CSV and chunks by rows, bypassing Docling's slow table parsing. Sheet names are prepended to each chunk for multi-sheet workbooks so context survives embedding. `_is_likely_header()` and `_find_header_row_index_from_rows()` locate the first actual header row, skipping sparse title/banner rows at sheet start — chunks now start at the real data table instead of embedding metadata rows as content.

### Batched S3 Vectors writes

Replaces single-batch vector upload with batched processing (50 vectors per batch), preventing request-body-size failures when storing large numbers of embeddings. Progress logged at 500-vector intervals.

---

## Compaction, Cost, and Chat Reliability Fixes

- **Paused agent orphaned after resume** (#207). The agent cache keyed on the unbuilt `system_prompt` parameter, but the construction snapshot persisted the built prompt. Resume requests passed the built form back into `get_agent`, hashing to a different cache slot — resume rebuilt a fresh agent (cache MISS), left the paused agent stuck, and the next non-resume turn cache-hit the paused agent, triggering "must resume from interrupt with list of interruptResponse's". Fix: snapshot the unbuilt prompt so resume hashes to the same key. Defense in depth: when `get_agent` cache-hits a paused agent on a non-resume request, evict and rebuild instead of serving the stale state.
- **Cost summary `InvalidOperation` on breakdown dicts** (#208). The streaming path produces a cost breakdown dict (`{"total": ..., "inputCost": ...}`), which flowed through `cost = message_metadata.cost or 0.0` unchanged and hit `Decimal(str(cost_delta))` in the DynamoDB summary writer — only the rollup path crashed, so the summary was silently going stale. Two layers of defense: `_coerce_cost_total` normalizes dict/float/None/NaN/inf to a finite float before the summary call, and a boundary `_safe_decimal` in `dynamodb_storage` collapses bad values to `Decimal("0")` across five `cost_delta` / `cache_savings_delta` sites.
- **Converse-proxy SSE header flush** (#217). The `/chat/api-converse` proxy used `async with httpx.AsyncClient(...)` and returned a `StreamingResponse` from inside the block. When the handler returned, `__aexit__` closed the client, which made `httpx` drain the upstream's full response — buffering the entire SSE stream before headers flushed. Same bug Phase 4 hit on the BFF proxy. Mirrored the fix: `_build_upstream_client()` seam, manual lifecycle, close in the generator's `finally` (SSE) or after `aread()` (non-SSE / 4xx). API-key authenticated path, independent of the BFF migration.
- **Google hourly-reconsent loop** (#210, #245). AgentCore Identity's refresh flow was never getting a chance to run: the in-process token cache returned warmed entries past the upstream 3600s lifetime, and a 401 on the AfterToolCallEvent retry path was writing the durable disconnect flag, which pinned subsequent fetches to `force_authentication=True`. Three coordinated changes: TTL on the cache (default 3000s); stop writing the disconnect flag from the 401 retry (reserved for the explicit Disconnect button); always send `prompt=consent` on Google's `initiate_consent` path so Disconnect/Reconnect cycles actually re-issue a refresh token (Google only re-issues refresh tokens on subsequent grants if the consent screen is shown).

---

## Bug Fixes

- Shared BFF `CookieCodec` singleton across seal and unseal paths (see Phase 7 above)
- `preview-chat` test flake: `PreviewChatService` imported `fetchEventSource` directly while the spec mocked the module; the Angular vitest builder's shared worker pool sometimes resolved the production binding to a different `vi.fn()` instance than the spec captured, producing "expected 1, got 0" on ~20-30% of CI runs. Replaced with a `FETCH_EVENT_SOURCE` `InjectionToken` overridden via `TestBed.providers` — 25/25 consecutive runs green (was 6/20).
- Cost service spec: absorb stray `resource()` loader request under shared vitest mock pool (#225)
- CSRF assertion in preview-chat spec hardened against shared-mock pollution (fails with `toHaveBeenCalled` now instead of `Cannot read properties of undefined`)
- Scrubbed `AGENTCORE_RUNTIME_WORKLOAD_NAME` in `tests/apis/shared/oauth/conftest.py` — local `.env` with that var set was flipping `_resolve_workload_token` into the workload-mint branch instead of the cache-hit / consent-required branches eight tests wanted to exercise (#214)

---

## Security

- Pygments 2.19.2 → 2.20.0 (ReDoS in GUID-matching regex, Dependabot alert #71)
- BFF `return_to` control-byte bypass closed (C0 range rejection, see Phase 7)
- CodeQL remediation (#247): log-injection via user-controlled values, unused imports/locals in `infrastructure-stack.ts`, `unused-local-variable` dead-code sites, empty-except explanatory comments
- CodeQL and Dependabot workflows retargeted from `develop` to `main`

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| pillow | older | 12.2.0 |
| cryptography | older | 47.0.0 |
| python-multipart | older | 0.0.27 |
| aiohttp | older | 3.13.5 |
| pygments | 2.19.2 | 2.20.0 |
| @angular/core + packages | 21.2.7 | 21.2.11 |
| @angular/cdk | 21.2.5 | 21.2.9 |
| @angular/build, @angular/cli | 21.2.6 | 21.2.9 |
| @angular/compiler-cli | 21.2.7 | 21.2.11 |
| tailwindcss, @tailwindcss/postcss | 4.2.2 | 4.2.4 |
| vitest, @vitest/coverage-v8 | 4.1.2 | 4.1.5 |
| ngx-markdown | 21.1.0 | 21.2.0 |
| @ng-icons/core, @ng-icons/heroicons | 33.2.0 | 33.2.2 |
| postcss | 8.5.8 | 8.5.12 |
| jsdom | 29.0.1 | 29.1.0 |
| fast-check | 4.6.0 | 4.7.0 |
| uuid | 13.0.0 | 14.0.0 |
| @analogjs/vite-plugin-angular | 3.0.0-alpha.26 | 3.0.0-alpha.53 |
| @analogjs/vitest-angular | 3.0.0-alpha.26 | 3.0.0-alpha.30 |
| aws-cdk-lib | 2.248.0 | 2.251.0 |
| aws-cdk | 2.1117.0 | 2.1120.0 |
| @types/node (infra) | 25.5.2 | 25.6.0 |

Frontend transitive overrides: `vite >= 7.3.2`, `dompurify >= 3.4.0`, `lodash-es >= 4.18.0`, `hono >= 4.12.14`, `@hono/node-server >= 1.19.13`, `undici < 8.0.0` (jsdom compatibility), mermaid's nested `uuid` pinned to 14.0.0.

---

## Deployment Notes

This release is operationally significant — the BFF migration changes infrastructure, IAM, SSM, and several external contracts. Deploy order matters.

- **Infrastructure first.** New resources: `BFFSessionsTable`, `BFFCookieSigningKey` (KMS), `CognitoBFFAppClient` (with secret in Secrets Manager), `VoiceTicketReplayTable`, `VoiceTicketSigningSecret`. CloudFront `/api/*` behavior + rewrite function on the frontend distribution. SPA fallback moved from distribution-wide `errorResponses` to a viewer-request function on the S3 behavior. CloudFront `readTimeout` capped at 60s without a service-quota increase.
- **Infrastructure second pass after cutover.** The public PKCE Cognito client is removed in Phase 7. Any external consumer of the SSM parameters `/auth/cognito/app-client-id` or `/oauth/callback-url` must migrate off before this deploy — they're gone post-deploy. Migrate to `/auth/cognito/bff-app-client-id` and register a per-system callback URL of your own.
- **Environment variables.** New on app-api: `BFF_AUTH_CALLBACK_URL`, `BFF_POST_LOGIN_REDIRECT_URL`, `BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS`, `BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS`, `VOICE_TICKET_*`, `INFERENCE_API_URL`, `CDK_COGNITO_SUPPORTED_IDPS`. All documented in `.env.example` (previously zero coverage for the Cognito and BFF blocks).
- **Cognito callback/logout URL registration.** Ensure the BFF client's `callbackUrls` and `logoutUrls` cover every environment you deploy to. Trailing commas in `CDK_COGNITO_CALLBACK_URLS` / `CDK_COGNITO_LOGOUT_URLS` are now trimmed; prior to this release they produced empty strings Cognito rejected with a regex validation error.
- **`CDK_CERTIFICATE_ARN` is required for the frontend stack** so the `/api/*` CloudFront origin uses `HTTPS_ONLY`. Without it the ALB HTTP listener 301-redirects to its public hostname and breaks same-origin cookie assumptions.
- **Frontend build.** CI must set `BUILD_CONFIG=production` for cloud builds. The `develop`-branch default previously bundled `environment.ts` with `localhost:8000`, which Private Network Access blocks.
- **External Bearer callers migrate endpoint.** The legacy in-process agent loop moved from `POST /chat/stream` to `POST /chat/agent-stream`. API-key and scripted callers against `/chat/stream` now hit the cookie-authenticated BFF proxy (which will 401 without a session).
- **`/chat/proxy-stream` is deleted.** Any caller on that path during the rolling-deploy window must move to `/chat/stream`.
- **SPA OAuth callback path deleted.** Third-party tools that deep-linked to `{spa}/auth/callback` must use the BFF path at `${appApiUrl}/auth/callback`.
- **`/config.json` is no longer deployed.** The `BucketDeployment` is gone; no CloudFront invalidation is needed for it. `cognitoDomainUrl` is served on demand from `GET /admin/auth-providers/cognito-redirect-uri` (admin-only).
- **Voice mode** requires the new `VOICE_TICKET_*` env vars and IAM grants on app-api. The SPA is wired to the WebSocket-ticket proxy automatically; no frontend config required.
- **Backend module paths.** `apis.app_api.costs`, `apis.app_api.tools.models`, `apis.app_api.storage`, and `apis.app_api.auth.api_keys` are gone. Any out-of-tree imports must move to `apis.shared.*`.

---

# Release Notes — v1.0.0-beta.23

**Release Date:** April 29, 2026
**Previous Release:** v1.0.0-beta.22 (April 8, 2026)

---

## Highlights

This release introduces **WebSocket voice streaming** with Nova Sonic bidirectional audio, a **multi-agent architecture** with pluggable agent types (Chat, Skill, Voice), **external MCP connectors via AgentCore Identity** replacing the bespoke OAuth token vault, **per-tool approval gates** for dangerous operations, and a full **Playwright E2E testing suite**. The agent layer has been refactored into a BaseAgent → ChatAgent hierarchy with a factory registry, enabling runtime agent-type selection. The legacy in-house OAuth flow (token vault, PKCE service, encryption layer) has been retired in favor of AgentCore Identity's managed credential providers. 252 files changed across 23,000+ lines of new code.

---

## Voice Mode — Bidirectional Audio Streaming

Full-stack voice interaction using Amazon Nova Sonic 2 via the Strands `BidiAgent`. Users can speak to the agent and receive spoken responses in real time, with voice-text continuity that carries context from prior text conversations into voice sessions.

### Backend

- `VoiceAgent(BaseAgent)` wraps `BidiAgent` with `BidiNovaSonicModel` for configurable voice, sample rate, and model selection
- Voice-text continuity via `_load_text_history()` — loads the text session's message history so the voice agent has full conversational context
- Separate `agent_id` ("voice") prevents session state conflicts between text and voice turns
- Voice-optimized system prompt with conversational guidelines
- WebSocket endpoint at `/voice/stream` (inference API) with JWT auth from query params
- Bidirectional protocol: audio/text input from client, agent event streaming back
- Accept-first WebSocket pattern aligned with the `sample-strands-agent-with-agentcore` reference architecture — AgentCore validates auth at the proxy layer
- Config message supplements missing params in cloud mode; `/voice/stream` for local dev, `/ws` alias for AgentCore Runtime
- Debug endpoints: `GET /voice/sessions`, `DELETE /voice/sessions/{id}`
- `CancelledError` handling in `VoiceAgent.stop()` for clean teardown of Nova Sonic streams
- Real-time cost calculation and token usage metadata for voice turns
- Log injection prevention via `_sanitize_log()` for all user-provided values in voice routes

### Frontend

Three-layer voice architecture in `session/services/voice/`:

- `pcm-utils.ts`: Pure PCM encoding/decoding (Float32 ↔ Int16 ↔ base64)
- `AudioRecorderService`: Mic capture via Web Audio API → 16kHz PCM chunks using an AudioWorklet (`pcm-capture.worklet.js`)
- `AudioPlayerService`: Gapless base64 PCM playback with interruption support
- `VoiceChatService`: WebSocket orchestration + state machine (idle → connecting → listening → speaking)
- `VoiceOverlayComponent`: Full-screen voice UI with visualizer orb and status badges
- Chat input gains a voice toggle button with animated state indicators (pulsing red = listening, bouncing green = speaking, spinner = connecting)
- Live transcript overlay during voice mode
- `MessageMapService.addVoiceMessage()` persists finalized voice transcripts to the message list

### Infrastructure

- `strands-agents[bidi]` optional dependency group added to `pyproject.toml`
- Inference API Dockerfile updated with `bidi` dependency in `uv sync` commands
- `InferenceApiStack` gains HTTP protocol configuration for WebSocket support
- Voice router registered in inference API `main.py`

### Test Coverage

16 new VoiceAgent unit tests, 14 voice route tests covering WebSocket auth, bidirectional streaming, and teardown.

---

## Multi-Agent Architecture

The monolithic `MainAgent` has been decomposed into a pluggable agent hierarchy with a factory registry, enabling runtime selection of agent behavior without redeployment.

### Agent Hierarchy

- `BaseAgent` (ABC): Shared initialization for model config, tools, session management, streaming, and approval hooks
- `ChatAgent(BaseAgent)`: Strands Agent creation and text streaming — the standard conversational agent
- `MainAgent(ChatAgent)`: Backward-compatible alias so all existing callers work unchanged
- `SkillAgent(ChatAgent)`: Progressive skill disclosure (see below)
- `VoiceAgent(BaseAgent)`: Bidirectional audio via BidiAgent (see Voice Mode above)

### Agent Type Registry

`agent_types.py` provides a pluggable registry pattern:

- `create_agent(agent_type, **kwargs)` → `BaseAgent` subclass
- `register_agent_type(name, cls)` for dynamic registration
- `ChatAgent` registered as `"chat"`, `SkillAgent` as `"skill"`, `VoiceAgent` as `"voice"` (conditional on `strands-agents[bidi]`)

### Factory Routing

The inference API now routes chat turns through `create_agent(agent_type, ...)` instead of hard-coding `MainAgent`. `InvocationRequest` gains an optional `agent_type` field, folded into the LRU cache key so chat/skill agents for the same session don't collide. `PausedTurnSnapshot` persists the resolved agent type so OAuth-paused turns rebuild on the correct factory variant after cache eviction.

### Configuration Centralization

All environment variables and magic strings consolidated into `agents/main_agent/config/constants.py` with `EnvVars`, `Defaults`, and `Prefixes` classes. 13 modules updated to import from the centralized constants instead of inline `os.getenv()` with hardcoded strings.

### Test Coverage

9 factory tests, 38 skill tests, 16 voice tests, plus existing 543 tests passing with zero behavior change.

---

## Progressive Skill Disclosure

A three-level skill architecture adapted from the `sample-strands-agent` reference, allowing the agent to discover and load tool capabilities on demand rather than loading everything upfront.

### How It Works

- **Level 1**: Lightweight skill catalog injected into the system prompt — the agent sees what skills exist without loading their full instructions
- **Level 2**: `skill_dispatcher` loads the full `SKILL.md` instructions on demand when the agent decides to use a skill
- **Level 3**: `skill_executor` runs the actual tool functions bound to the skill

### New Modules

- `skills/skill_registry.py`: Discovers `SKILL.md` files, binds tools, serves the catalog
- `skills/skill_tools.py`: `skill_dispatcher` + `skill_executor` as Strands `@tool` functions
- `skills/decorators.py`: `@skill()` decorator and `register_skill()` for tool tagging
- `skill_agent.py`: `SkillAgent(ChatAgent)` with progressive disclosure override

### Skill Definitions

- `web-search/SKILL.md`: Example skill definition for web search tools
- `canvas-morning-check/SKILL.md`: Educator-facing morning course health check that surfaces submission rates, struggling students, and upcoming deadlines via the Canvas MCP server, with FERPA-aware anonymization guidance

---

## External MCP Connectors via AgentCore Identity

The bespoke OAuth token vault (per-user DynamoDB encryption, KMS, Secrets Manager client credentials, manual refresh) has been replaced with AgentCore Identity's managed token vault and credential providers. This is a full-stack rewrite of how external MCP tools authenticate with third-party services.

### AgentCore Identity Integration

- `AgentCoreContextMiddleware` copies Runtime headers (`WorkloadAccessToken`, `OAuth2CallbackUrl`, session ID, request ID) into `BedrockAgentCoreContext` on every invocation — required because the Inference API is a plain FastAPI app, not a `BedrockAgentCoreApp`
- `AgentCoreIdentityClient` wraps `IdentityClient.get_token()` with a narrower surface for `USER_FEDERATION` (3LO) flows, surfacing "user consent required" as a structured `TokenResult(authorization_url=...)` rather than an exception
- `AgentCoreCredentialProviderRegistrar` wraps `bedrock-agentcore-control` for admin-side OAuth2 credential provider CRUD with vendor mapping (Google/Microsoft/GitHub to native vendors; Canvas/Custom via OIDC discovery URL)

### OAuth Consent Flow

When an external MCP tool needs OAuth consent, the authorization URL flows through the SSE stream as an `oauth_required` event:

- `OAuthConsentService` orchestrates popup opening + `postMessage` receipt
- `OAuthConsentBanner` renders a "Connect" button inline in the chat
- `/oauth-complete` landing page handles the AgentCore callback redirect and signals consent completion to the opener tab
- `PendingInterrupt` gains an `oauth_consent` variant so the consent prompt rehydrates after a page refresh

### Legacy OAuth Retirement

Deleted: `OAuthService`, `OAuthTokenRepository`, `token_cache.py`, encryption layer, user-facing `/oauth/*` routes, `OAuthToolService`, settings/connections page, settings/oauth-callback page. The admin UI has been rebranded from "OAuth Providers" to "Connectors" (`admin/connectors/`), with the form rewritten for the AgentCore-owned shape — credential rotation requires `clientId` + `clientSecret` together (AgentCore's update API is not partial), and the success screen displays the AgentCore callback URL with a copy button.

### Shared Workload Identity

A `CfnWorkloadIdentity` (`<projectPrefix>-platform-workload`) is provisioned in `InfrastructureStack` and shared between inference-api and app-api. Both services mint user-scoped workload tokens against it via `GetWorkloadAccessTokenForUserId`, ensuring the OAuth token vault is keyed consistently — a user consents once and both code paths find the token. The runtime's auto-created identity stays in place but is no longer used for vault calls.

### Infrastructure

- `InfrastructureStack`: New `CfnWorkloadIdentity` + SSM exports
- `AppApiStack`: IAM grants for Secrets Manager lifecycle (create/update/delete/get) on `bedrock-agentcore-identity!default/oauth2/*`, plus `bedrock-agentcore:GetResourceOauth2Token`
- `InferenceApiStack`: Runtime workload identity lookup via `AwsCustomResource` (SDK `GetAgentRuntime` call) replacing the broken `Fn::GetAtt` on nested attribute paths; IAM grants for OAuth secret read
- CloudFront added to API CORS origins

### Test Coverage

278 lines of AgentCore Identity client tests, 245+ lines of external MCP client tests, 787 lines of OAuth consent hook tests, 456 lines of connector route tests, 403 lines of AgentCore registrar tests, 189 lines of context middleware tests, 179 lines of tool freshness tests, 400 lines of session metadata tests, plus updated model and repository tests.

---

## Per-Tool MCP Approval Gate

Replaces the hardcoded `EmailApprovalHook` / `ExternalWriteApprovalHook` / `DangerousToolApprovalHook` with a single `MCPExternalApprovalHook` whose gating set is sourced from per-tool `needs_approval` flags in the tool catalog.

### How It Works

- Admins toggle approval per tool in the catalog via the tool form
- The hook surfaces a `tool_approval_required` SSE event when a gated tool is invoked
- The frontend renders an inline approve/decline prompt (`ToolApprovalPromptComponent`)
- The user's decision resumes the paused turn via the Strands interrupt protocol
- `PendingInterrupt` gains a `tool_approval` variant so the prompt rehydrates after a page refresh

### Admin Tool Discovery

A new `POST /admin/tools/discover` endpoint calls the MCP server's tool listing to populate tool entries without manual typing, reducing configuration friction for external MCP tools.

### Paused Turn Snapshot Refactor

`_persist_paused_turn_snapshot` extracted as a dedicated helper called once from the `done` branch, so any interrupt flavor (OAuth consent, tool approval, future variants) gets a snapshot without depending on the OAuth extractor running first.

---

## Tool Catalog Simplification

The "Sync from Registry" admin feature has been removed in favor of DynamoDB as the single source of truth for the tool catalog.

- Code-defined tools are now seeded by the bootstrap script (expanded to cover `calculator` and `generate_diagram_and_validate`)
- Admins add everything else through the "Add Tool" form
- The in-memory fallback in `ToolCatalogService` has been removed
- The stale `get_current_weather` local tool has been deleted
- `ToolAccessService.filter_allowed_tools` now sources its catalog from a TTL-cached DynamoDB snapshot (`freshness.get_all_tool_ids`) instead of the legacy in-memory catalog, fixing an issue where MCP-external and A2A tools added via the admin form were silently filtered out for wildcard-access users
- Admin create/update/delete invalidate the snapshot so changes are visible on the next chat turn

---

## E2E Testing

A comprehensive Playwright E2E test suite covering authentication, navigation, chat, settings, assistants, and session management.

### Test Coverage

3,400+ lines of new E2E tests across 12 spec files:

- `login.spec.ts`: Authentication flows including Cognito login
- `navigation.spec.ts`: Route navigation and guards
- `not-found.spec.ts`: 404 handling
- `admin-access.user.spec.ts`: Admin route protection
- `chat.user.spec.ts`: Chat interactions, message sending, model selection
- `error-handling.user.spec.ts`: Error state handling
- `file-upload-ui.user.spec.ts`: File upload UI interactions
- `model-selector.user.spec.ts`: Model dropdown behavior
- `settings-panel.user.spec.ts`: Settings panel interactions
- `manage-sessions.user.spec.ts`: Session list management
- `assistants.user.spec.ts`: Assistant CRUD operations
- Settings specs: appearance, chat preferences, profile, usage

### Infrastructure

- `playwright.config.ts` and `playwright.ci.config.ts` for local and CI environments
- Auth setup files (`auth-admin.setup.ts`, `auth-user.setup.ts`) with Cognito account provisioning
- `scripts/nightly/e2e-test.sh`: E2E runner with dynamic CloudFront URL discovery and Cognito callback URL registration
- `scripts/nightly/seed-e2e-users.sh`: Cognito user provisioning for nightly runs
- Seed script integrated into E2E workflow for bootstrap data

---

## Approval Hooks for Dangerous Tool Operations

Three approval hook categories following the `sample-strands-agent` pattern, all using Strands `BeforeToolCallEvent`:

- `EmailApprovalHook`: Gates `send_email`, `delete_emails`, `forward_email`, etc.
- `ExternalWriteApprovalHook`: Gates `create_pull_request`, `deploy`, `push_code`, etc.
- `DangerousToolApprovalHook`: Gates `delete_file`, `drop_table`, `execute_sql`, etc.

Hooks set `_approval_required` / `_approval_message` on the tool_use dict for the streaming layer to surface to the client. All hooks registered in `BaseAgent._create_hooks()` — inherited by all agent types.

Note: These category-based hooks were subsequently superseded by the per-tool MCP approval gate (see above), which provides finer-grained control via the tool catalog.

---

## UI Improvements

- **Copy agent response button**: New `MessageActionsComponent` with a copy-to-clipboard button on agent messages
- **Markdown links open in new tab**: `marked` renderer configured with `target="_blank"` and `rel="noopener noreferrer"` on all rendered links, preventing reverse-tabnabbing via `window.opener`

---

## Bug Fixes

- **Duplicate sidebar entries**: `ensure_session_metadata_exists` was using `put_item` with `attribute_not_exists(PK)`, but the main-table SK encodes `lastMessageAt` (rotated each turn), so the conditional always succeeded and the same session accumulated duplicate rows. Fixed by gating creation on a `SessionLookupIndex` GSI lookup instead
- **OAuth2CallbackUrl header stripping**: Frontend was appending `?provider_id=<name>` to the callback URL, which the middleware's redirect-pivot guard rejected. The append was redundant — the backend re-tags `provider_id` itself
- **Workload identity service-linking**: App-api was failing 500 on connector endpoints because `AGENTCORE_RUNTIME_WORKLOAD_NAME` pointed at the runtime's auto-created workload identity, which is service-linked and cannot mint tokens for cross-service callers
- **CloudFormation GetAtt on nested attributes**: `Fn::GetAtt(AgentCoreRuntime, 'WorkloadIdentityDetails.WorkloadIdentityArn')` rejected by CFN because the resource schema only declares the parent struct as a readonly attribute. Replaced with an `AwsCustomResource` SDK call
- **Delete-failed state resilience**: Added handling for documents stuck in `delete-failed` state

---

## CI/CD Improvements

- E2E testing integrated into nightly pipeline with dynamic CloudFront URL discovery, Cognito user provisioning, and callback URL registration
- Testing subdomain added to nightly deploy pipeline
- Seed script added to E2E workflow for bootstrap data provisioning

### GitHub Actions Updates

| Package | From | To |
|---|---|---|
| actions/cache | 5.0.4 | 5.0.5 |
| docker/build-push-action | 7.0.0 | 7.1.0 |
| actions/upload-artifact | 7.0.0 | 7.0.1 |
| github/codeql-action | 4.35.1 | 4.35.2 |
| aquasecurity/trivy-action | 0.35.0 | 0.36.0 |
| actions/setup-node | 6.3.0 | 6.4.0 |

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| fastapi | 0.135.3 | 0.136.1 |
| uvicorn | 0.44.0 | 0.46.0 |
| boto3 | 1.42.83 | 1.42.96 |
| authlib | 1.6.9 | 1.7.0 |
| strands-agents | 1.34.1 | 1.37.0 |
| strands-agents-tools | 0.3.0 | 0.5.1 |
| aws-opentelemetry-distro | 0.16.0 | 0.17.0 |
| bedrock-agentcore | 1.6.0 | 1.6.4 |
| openai | 2.30.0 | 2.32.0 |
| google-genai | 1.70.0 | 1.73.1 |
| pytest | 9.0.2 | 9.0.3 |
| hypothesis | 6.151.11 | 6.152.3 |
| ruff | 0.15.9 | 0.15.12 |
| mypy | 1.20.0 | 1.20.2 |

---

## Deployment Notes

This release includes new infrastructure resources and significant backend changes. Deploy order matters for the connector feature.

- **Infrastructure:** Deploy first. New `CfnWorkloadIdentity` resource for shared OAuth token vault. SSM parameters added under `/<projectPrefix>/oauth/platform-workload-identity-{name,arn}`.
- **Backend:** Restart both App API and Inference API containers. The inference API now requires the `bidi` dependency group (`uv sync --extra bidi`). The legacy OAuth service, token vault, and encryption layer have been removed — if you had custom integrations against `/oauth/*` endpoints, they no longer exist. Voice streaming is available at `/voice/stream` (WebSocket).
- **Frontend:** Full rebuild and deploy required. New voice overlay, connector admin pages, tool approval prompts, and E2E test infrastructure. The settings/connections page has been removed; users manage connector consent inline during chat.
- **Connectors:** If you had OAuth providers configured under the old system, you must re-register them as AgentCore Identity credential providers via the new admin Connectors page. The old token vault data is not migrated.
- **Tool Catalog:** The "Sync from Registry" feature is gone. Run the bootstrap seed script to populate code-defined tools, then use the admin "Add Tool" form for everything else.
- **Nightly/CI:** E2E tests require Playwright and Cognito user provisioning. See `scripts/nightly/e2e-test.sh` and `scripts/nightly/seed-e2e-users.sh`.

---

# Release Notes — v1.0.0-beta.22

**Release Date:** April 8, 2026
**Previous Release:** v1.0.0-beta.20 (April 1, 2026)

---

## Highlights

This release replaces the authentication system end-to-end with a **Cognito-native identity broker** and zero-configuration first-boot experience. The previous generic OIDC flow, backend token exchange, and manual auth provider seeding are gone entirely. Alongside the auth migration, **CORS handling is unified** across all six CDK stacks via a shared `buildCorsOrigins` helper, the **RBAC authorization layer is consolidated** to a single `require_app_roles` dependency with role enrichment from stored user profiles, and a **documentation cleanup** purges 54,000+ lines of outdated specs and AI-generated artifacts.

---

## ⚠️ Breaking Change — Cognito Authentication Migration

**This is a breaking change release.** The entire authentication system has been replaced with AWS Cognito as the sole identity broker. The previous generic OIDC implementation — including the backend token exchange service, OIDC discovery endpoint, PKCE flow, and multi-provider auth bootstrapping — has been removed. There is no backward compatibility layer and no migration path that preserves the old auth flow. The legacy implementation is not supported going forward.

**If you are upgrading an existing deployment**, you must:

1. Deploy the Infrastructure stack first to provision the new Cognito User Pool, App Client, and Domain
2. Reconfigure any federated identity providers (e.g., Entra ID, Okta) as Cognito federated IdPs — the old auth provider table format is not compatible
3. Re-bootstrap your admin user via the new first-boot flow (the first user to access the app after upgrade creates the admin account)
4. Update all CI/CD workflows with `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` environment variables

**If you are deploying fresh**, the new first-boot experience handles everything automatically — no manual seeding or Secrets Manager configuration required.

---

## Cognito First-Boot Authentication

The entire authentication architecture has been rearchitected around AWS Cognito as the native identity provider. The previous generic OIDC flow — including manual auth provider seeding, Secrets Manager client secret configuration, and the multi-step bootstrap process — has been removed with no backward compatibility.

### First-Boot Experience

On initial deployment, the first user to access the application is presented with a setup page to create the admin account directly in Cognito. This eliminates the previous multi-step bootstrap process (seed auth provider secrets, configure OIDC endpoints, create initial user). The first-boot flow uses race-condition-safe DynamoDB writes to ensure only one admin account is created.

### Infrastructure

A Cognito User Pool, App Client, and Domain are now provisioned in the Infrastructure CDK stack. SSM parameters wire the Cognito configuration across stacks. The AgentCore Runtime is configured with a single Cognito JWT authorizer, replacing the previous generic OIDC validator.

### Backend

- New `CognitoJWTValidator` replaces `GenericOIDCJWTValidator` with Cognito-specific JWKS validation and claim extraction
- New `system/` module (`cognito_service.py`, `repository.py`, `routes.py`, `models.py`) handles first-boot setup, system status, and Cognito user/group management
- New `cognito_idp_service.py` in `shared/auth_providers/` manages federated identity provider CRUD via Cognito IdP APIs
- `add_user_to_group` method manages Cognito group membership with rollback on failure
- Bootstrap script (`seed_bootstrap_data.py`) simplified — no longer seeds auth provider secrets, focuses on RBAC roles and JWT mappings
- Runtime-provisioner and runtime-updater Lambda functions removed entirely (2,800+ lines deleted)

### Frontend

- New first-boot page (`first-boot.page.ts`) with admin account creation form and `first-boot.guard.ts` route guard
- Login page simplified — delegates to Cognito OAuth 2.0 + PKCE flow instead of managing tokens directly
- `auth-api.service.ts` removed — frontend communicates directly with Cognito
- `callback.service.ts` rewritten for Cognito token exchange
- Auth provider form now displays the required Cognito redirect URI (`{cognitoDomainUrl}/oauth2/idpresponse`) with a copy button for zero-friction IdP registration
- Provider list page simplified — runtime status UI and unused icon imports removed
- Updated favicon and logo assets with refreshed branding and cross-platform icon support

### Test Coverage

1,177 lines of new `CognitoIdPService` tests, 316 lines of `CognitoJWTValidator` tests, 286 lines of first-boot tests, 278 lines of system service tests, plus updated auth route, dependency, RBAC, and auth sweep tests. Frontend gains `SystemService` unit tests and updated auth guard/callback/interceptor specs.

---

## Cognito-Managed Auth Flow Migration

The backend OIDC authentication service and token exchange layer have been removed entirely with no compatibility shim. The frontend now communicates directly with Cognito for all auth operations. The legacy OIDC implementation is not supported and will not be restored.

### Removed

- Backend `auth/models.py`, `auth/service.py`, and associated test files (`test_oidc_auth_service.py`, `test_pkce.py`)
- Token refresh and logout endpoints from backend auth routes
- OIDC discovery endpoint (`POST /discover`) from admin auth provider routes
- 1,318 lines of backend auth code deleted

### Simplified

- Auth routes reduced to a single public provider listing endpoint
- User service updated to work with Cognito-provided user information
- Auth provider repository gains JSON parsing error handling for malformed Secrets Manager values

---

## RBAC Authorization Consolidation

The authorization system has been consolidated from multiple role-checking functions to a single `require_app_roles` dependency that resolves permissions through `AppRoleService`.

### Removed

- `require_roles`, `require_all_roles`, `has_any_role`, `has_all_roles`
- Role-specific decorators: `require_faculty`, `require_staff`, `require_developer`, `require_aws_ai_access`
- Auth module exports simplified to only `require_app_roles` and `require_admin`

### Added

- User roles enriched from stored DynamoDB profile during token processing, ensuring RBAC uses correct IdP-mapped roles instead of Cognito provider group names
- User profile cache invalidation on `sync_my_profile` — subsequent requests pick up fresh roles immediately instead of waiting for the 5-minute cache TTL
- JSON array parsing for `custom:roles` claim (`CognitoJWTValidator`) — supports both `'["Admin","Staff"]'` and comma-separated formats for Entra ID role mapping
- `parseRolesFromToken` utility function on the frontend with 118 lines of test coverage
- `jwt_role_mappings` updates now allowed on `system_admin` role — validation changed from error-raising to silent field filtering with logging
- Role priority maximum increased from 999 to 1000

---

## CORS Unification

All six CDK stacks now use a single shared `buildCorsOrigins()` helper in `config.ts` that builds CORS origins from `CDK_DOMAIN_NAME` (always), `localhost:4200` (always, for local dev), and optional per-section `additionalCorsOrigins`. This replaces the previous per-stack `corsOrigins` fields that were inconsistent and error-prone.

### Changes

- S3 CORS configuration made conditional — `undefined` when no origins are configured, preventing empty CORS rules
- RAG CORS Lambda fix: `ExposedHeaders` corrected to `ExposeHeaders` (the valid boto3 S3 CORS parameter name), fixing CloudFormation custom resource failures during frontend stack deployment
- Both Python APIs (`app_api`, `inference_api`) read `CORS_ORIGINS` env var, replacing hardcoded `allow_origins=['*']` with an env-driven allowlist
- Regression tests added for CORS_ORIGINS in app-api and inference-api stack tests

---

## Bootstrap & Seeding Fixes

- Bootstrap script (`seed_bootstrap_data.py`) is now the sole owner of RBAC role seeding — `ensure_system_roles()` removed from app-api startup to prevent overwriting admin customizations on every boot
- `system_admin` role seeded with `jwt_role_mappings=['system_admin']` instead of empty array — fixes the issue where Cognito first-boot admin users had the right `cognito:groups` claim but no matching AppRole
- Additive JWT mapping seeding: if the role exists but is missing required mappings, they're added without removing existing custom mappings

---

## CI/CD Improvements

- `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` added to all workflow jobs that run synth or deploy (previously missing from `inference-api.yml` and `gateway.yml`, causing `loadConfig` validation failures)
- `CDK_CORS_ORIGINS` and `CDK_FILE_UPLOAD_CORS_ORIGINS` added to nightly deploy pipeline
- SSM `StringParameter` creation guarded with conditional check to prevent empty string values (SSM parameter tier rejects empty strings)
- File upload CORS validation softened from hard error to warning since `loadConfig` runs for all stacks
- Infrastructure workflow updated with Cognito context values
- Trivy image scanning action upgraded from `v0.28.0` to `v0.35.0` with corrected SHA pin — the previous pin (`18f2510`) was actually the `v0.29.0` commit SHA mislabeled as `v0.28.0`, and was among the tags compromised in the [March 2026 trivy-action supply chain attack](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23). The new pin (`57a97c7e`) points to the post-remediation immutable `v0.35.0` release
- App API `synth-cdk` job now actually skipped on pull requests — the `if: github.event_name != 'pull_request'` guard was missing despite being documented in beta.20. PRs no longer require AWS credentials or ARM runners for the app-api workflow

---

## Bug Fixes

- Model form validation summary now displayed above submit button showing all invalid fields — fixes the greyed-out submit button with no visible errors on edit
- "Add Model" button and "Browse Bedrock/Gemini/OpenAI Models" links uncommented on manage models page
- `SystemService` tests stabilized against shared fetch spy by filtering assertions by URL
- Inference API endpoints updated with `/invocations` path and URL-encoded ARN to prevent parsing errors with AgentCore runtime ARNs
- ALB listener rule updated with `requestHeaderConfiguration` to propagate `Authorization` header to inference API
- AWS Marketplace permissions (`ViewSubscriptions`, `Subscribe`) added to runtime execution role for marketplace-gated Bedrock models

---

## Documentation Cleanup

54,665 lines of outdated AI specs, feature summaries, and documentation purged across 121 files. Removed content includes completed spec directories (agent-core-tests, api-route-tests, auth-rbac-tests, bootstrap-data-seeding, config-cleanup-audit, environment-agnostic-refactor, and 12 others), duplicate docs under `docs/specs/`, the `GEMINI.md` agent config, `codeql-alerts.json` dump, and the `CODE_REVIEW_TOKEN_STORAGE.md` document. The Cognito first-boot auth and reliable document deletion specs were added as replacements.

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| Angular packages | 21.2.6 | 21.2.7 |
| @angular/cdk | 21.2.4 | 21.2.5 |
| @angular/build | 21.2.5 | 21.2.6 |
| @angular/cli | 21.2.5 | 21.2.6 |
| katex | 0.16.44 | 0.16.45 |
| marked | 17.0.5 | 17.0.6 |
| mermaid | 11.13.0 | 11.14.0 |
| @analogjs/vite-plugin-angular | 3.0.0-alpha.18 | 3.0.0-alpha.26 |
| @analogjs/vitest-angular | 3.0.0-alpha.18 | 3.0.0-alpha.26 |
| aws-cdk-lib | 2.245.0 | 2.248.0 |
| aws-cdk (CLI) | 2.1115.0 | 2.1117.0 |
| @types/node | 25.5.0 | 25.5.2 |
| ts-jest | 29.4.6 | 29.4.9 |
| fastapi | 0.135.2 | 0.135.3 |
| uvicorn | 0.42.0 | 0.44.0 |
| boto3 | 1.42.78 | 1.42.83 |
| strands-agents | 1.33.0 | 1.34.1 |
| bedrock-agentcore | 1.4.8 | 1.6.0 |
| google-genai | 1.69.0 | 1.70.0 |
| hypothesis | 6.151.10 | 6.151.11 |
| ruff | 0.15.8 | 0.15.9 |
| mypy | 1.19.1 | 1.20.0 |

---

## Deployment Notes

**This release contains breaking changes.** See the migration steps at the top of this document.

- **Infrastructure:** Deploy first. The stack now provisions a Cognito User Pool, App Client, and Domain. New CDK context values required: `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` must be set in all workflow environments.
- **Backend:** The App API no longer handles token exchange or OIDC discovery. The `GenericOIDCJWTValidator`, `auth/service.py`, `auth/models.py`, and all token management endpoints have been deleted. The `runtime-provisioner` and `runtime-updater` Lambda functions have been removed. Restart all containers.
- **Frontend:** Full rebuild and deploy required. The auth flow now uses Cognito OAuth 2.0 + PKCE directly. The `auth-api.service.ts` has been removed. The first user to access a fresh deployment will see the first-boot setup page.
- **Federated IdPs:** Existing Entra ID, Okta, or other OIDC providers must be reconfigured as Cognito federated identity providers. The old auth provider table format and Secrets Manager secret structure are no longer used. Register the Cognito redirect URI (`{cognitoDomainUrl}/oauth2/idpresponse`) in your external IdP.
- **Bootstrap:** The seed script no longer seeds auth provider secrets or OIDC configuration. It only handles RBAC roles and JWT mappings.
- **Nightly/CI:** All workflows now require `CDK_DOMAIN_NAME` and `CDK_CORS_ORIGINS` environment variables.

---

# Release Notes — v1.0.0-beta.20

**Release Date:** April 1, 2026
**Previous Release:** v1.0.0-beta.19 (March 25, 2026)

---

## Highlights

This release delivers **reliable document deletion** with a soft-delete lifecycle and background cleanup, a **displayText system** that preserves original user messages when RAG augmentation or file attachments modify the prompt, a **fine-tuning cost dashboard** for admin visibility into SageMaker training spend, and a major **dependency refresh** across all three ecosystems via Dependabot. The security and code quality hardening from the initial beta.20 scope is also included — all CodeQL findings resolved, four Dependabot security vulnerabilities patched, cyclic imports eliminated, and silent exception swallowing replaced with proper logging.

---

## Reliable Document Deletion

Document deletion has been rearchitected with a soft-delete pattern and background cleanup to prevent orphaned S3 objects and vector embeddings.

### Soft-Delete Lifecycle

Documents now transition through a `deleting` status before removal. The delete endpoint marks the document immediately and returns, while cleanup runs asynchronously. A DynamoDB TTL field (7-day expiry) acts as a backstop for failed cleanups.

### Cleanup Service

A new `cleanup_service.py` handles retry logic for S3 vector deletion and source file removal. Deterministic vector key generation ensures reliable cleanup even if the original ingestion metadata is incomplete.

### Search Filtering

The search path now filters out non-complete documents, preventing stale results from appearing when a document is mid-deletion. The RAG service cross-checks document status during search.

### Assistant Deletion

When an assistant is deleted, all associated documents are batch soft-deleted with background cleanup. A new `delete_vectors_for_assistant` function removes embeddings from the vector store by assistant ID.

### Upload Failure Reporting

A new `POST /{document_id}/upload-failed` endpoint allows the frontend to report client-side upload errors, marking documents as failed with error details for debugging.

### Test Coverage

4,200+ lines of new tests across property-based tests (cleanup service, document deletion, search filtering, vector deletion) and integration tests (delete endpoints, cleanup service, document deletion flows).

---

## DisplayText for RAG-Augmented and File Attachment Messages

When RAG augmentation or file attachments modify the user's prompt before sending it to the agent, the original message text is now preserved and displayed in the UI instead of the augmented version.

### How It Works

- The `stream_async` and `StreamCoordinator` accept an `original_message` parameter to capture the user's input before modification
- When the original differs from the augmented version, a `displayText` metadata record (`D#` prefix) is stored in DynamoDB alongside the cost record
- The metadata retrieval path queries both cost records (`C#`) and display text records (`D#`)
- The frontend `user-message` component renders `displayText` when available, falling back to the stored message content

### Debug Output Toggle

A new `showDebugOutput` setting in Chat Preferences lets users toggle visibility of debug information, useful for inspecting what the agent actually received versus what the UI displays.

---

## Fine-Tuning Cost Dashboard

A new admin page provides visibility into SageMaker fine-tuning costs and usage.

### Admin Cost Endpoint

`GET /admin/fine-tuning/costs` returns aggregated cost data for fine-tuning jobs, with per-user breakdowns showing training hours consumed and quota utilization.

### Default Quota Hours

Fine-tuning access control now supports a default monthly quota for users without explicit grants, configurable via `CDK_FINE_TUNING_DEFAULT_QUOTA_HOURS` in the infrastructure config.

### Frontend

A dedicated `/admin/fine-tuning-costs` page displays cost summaries, per-user breakdowns, and usage statistics with period selection.

### Fine-Tuning Dashboard Polish

The fine-tuning dashboard also received an informational section explaining the fine-tuning workflow and updated icons for better visual clarity.

---

## Assistant Simplification

### Archive Removal

The assistant archive functionality has been removed entirely. The `ARCHIVED` status, `archive_assistant` endpoint, and `include_archived` query parameter are gone. Assistants now have a single delete operation — simpler lifecycle, less code.

---

## Conversation Sharing Fixes

### Shared Conversation Deletion

Deleting a session now properly cascades to associated shared conversations. The shares service cleans up all share records when the parent session is deleted, and the frontend session list reflects the deletion state correctly.

### Message Export Fix

The share export feature (`POST /shares/{share_id}/export`) was failing to persist messages to AgentCore Memory. Fixed by switching from the deprecated `append_message` API to `create_message` with proper `SessionMessage` wrapping and index-based ordering.

### UI Improvements

- Shared conversation header simplified — metadata and export button repositioned for cleaner layout
- Export button moved to a floating action bar at the bottom of the shared view
- Icon updates: share icon replaced with `heroAdjustmentsHorizontal` in session management, `heroChatBubbleLeftRight` in shared view header

---

## Testing Infrastructure

### Analog.js Migration

Frontend testing has been migrated to Analog.js tooling (`@analogjs/vite-plugin-angular` and `@analogjs/vitest-angular` v3.0.0-alpha.18). The standalone `vitest.config.ts` has been removed in favor of Analog.js configuration. Analog.js dependencies are pinned to exact versions per the supply chain policy.

### Property-Based Testing

`fast-check` has been added as a dev dependency (v4.6.0, exact pin) for property-based testing in the frontend test suite.

---

## Security Vulnerability Patches

Four Dependabot-flagged vulnerabilities have been patched across all three package ecosystems:

| Package | Version Change | Severity | Issue |
|---------|---------------|----------|-------|
| `requests` (Python) | 2.32.5 → 2.33.0 | Medium | Insecure temp file reuse in `extract_zipped_paths()` |
| `picomatch` (frontend) | 4.0.3 → 4.0.4 | High / Medium | ReDoS via extglob quantifiers; method injection in POSIX character classes |
| `picomatch` (infrastructure) | 2.3.1 → 2.3.2 | Medium | Method injection in POSIX character classes |
| `diff` (infrastructure) | patched | Low | DoS in `parsePatch` / `applyPatch` |

Frontend and infrastructure `picomatch` fixes use npm `overrides` to force patched versions through transitive dependency trees (`@angular-devkit/core`, `@angular/build`).

**Known unfixable:** `yaml@1.10.2` is bundled inside `aws-cdk-lib@2.244.0` (latest) — awaiting an AWS CDK update. `Pygments@2.19.2` (latest) has no patched version yet.

---

## CodeQL Remediation — All Findings Resolved

Two passes resolved every open CodeQL finding on `develop`, covering 130+ files across Python, TypeScript, and GitHub Actions.

### Log Injection (180 fixes)

User-controlled values removed from f-string log statements across the entire backend. All logging now uses `%s`-style parameterized formatting, preventing log injection attacks where user input could forge log entries.

### Silent Exception Swallowing (5 fixes)

Empty `except: pass` blocks — a recurring source of hidden bugs — have been eliminated:

- **`event_formatter.py`** — Errors during final result extraction now log a warning instead of vanishing silently. This was masking streaming failures that were impossible to diagnose.
- **`url_fetcher.py`** — Bare `except:` (catching `BaseException` including `KeyboardInterrupt`) narrowed to `Exception` with an explanatory comment.
- **`code_interpreter_diagram_tool.py`** — Same bare `except:` fix as above.
- **`admin/users/service.py`** — Invalid pagination cursors now log a warning instead of silently resetting to page 1.
- **`tool_result_processor.py`** — `JSONDecodeError` catch annotated with intent comment.

### Cyclic Import Eliminated

The circular dependency between `metadata_storage.py` and `dynamodb_storage.py` has been broken by moving the `get_metadata_storage()` factory function to the package `__init__.py`. The dependency graph is now one-directional:

```
storage/__init__.py (factory) → dynamodb_storage.py → metadata_storage.py (ABC)
```

Three callers updated to import from `apis.app_api.storage` instead of `apis.app_api.storage.metadata_storage`.

### Other Fixes

- **Unreachable code** — Dead `if result_seen: break` removed from `stream_processor.py` (`result_seen` was initialized to `False` and never set to `True`)
- **Redundant assignment** — Unused `job =` on `create_inference_job()` call removed in fine-tuning routes
- **Print during import** — `print()` statements in `inference_api/main.py` replaced with `logging`
- **Commented-out code** — Stale `InvocationRequest` class removed from inference API models
- **Unnecessary lambdas** — `lambda v: int(v)` simplified to `int` in fine-tuning repositories
- **13 unused local variables** removed across 10 files
- **3 unused imports** removed (including dead re-exports in `bedrock_embeddings.py`)

### False Positives Dismissed (11 alerts)

- 9× `actions/untrusted-checkout` on nightly workflows — these are schedule/dispatch only, never triggered by PRs
- 1× `py/non-iterable-in-for-loop` — iterating over `Enum` members is valid Python
- 1× `py/unused-global-variable` — `_generic_validator_initialized` is used via `global` statement (CodeQL doesn't track this)

---

## RAG Ingestion Fixes

### Lambda Image Digest Refresh

Fixed an issue where RAG ingestion Lambda deployments would report "no changes" even after pushing a fresh Docker image. The root cause: CDK resolves the image tag via SSM at synth time, and if the tag hasn't changed (only the underlying layers), CloudFormation sees no diff. The deploy script now explicitly calls `update-function-code` after image push to force a digest refresh, with a wait condition to ensure the update completes.

### Shared Embeddings Module

Added the shared embeddings package to the RAG ingestion Lambda Docker image, resolving import errors when `bedrock_embeddings.py` attempted to load re-exported functions from `apis.shared.embeddings`.

---

## CI/CD Improvements

### PR Workflow Optimization

CDK synthesis (`synth-cdk`) is now skipped on pull requests in the app-api workflow, matching the existing pattern for Docker builds and deployments. PRs no longer require AWS credentials for the synth step.

### GitHub Actions Updates

- `actions/upload-artifact` upgraded from 6.0.0 to 7.0.0
- `actions/download-artifact` upgraded from 7.0.0 to 8.0.1
- `actions/setup-node` upgraded from 5.0.0 to 6.3.0
- `github/codeql-action` upgraded to latest SHA

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| uvicorn | 0.35.0 | 0.42.0 |
| boto3 | 1.42.73 | 1.42.78 |
| strands-agents | 1.32.0 | 1.33.0 |
| strands-agents-tools | 0.2.23 | 0.3.0 |
| aws-opentelemetry-distro | 0.14.2 | 0.16.0 |
| bedrock-agentcore | 1.4.7 | 1.4.8 |
| openai | 2.29.0 | 2.30.0 |
| google-genai | 1.68.0 | 1.69.0 |
| cachetools | 7.0.5 | 6.2.4 (downgraded for aws-opentelemetry-distro compatibility) |
| hypothesis | 6.151.9 | 6.151.10 |
| ruff | 0.15.7 | 0.15.8 |
| Angular packages | 21.2.5 | 21.2.6 |
| @angular/cdk | 21.2.3 | 21.2.4 |
| @angular/build | 21.2.3 | 21.2.5 |
| @angular/cli | 21.2.3 | 21.2.5 |
| ng2-charts | bumped | latest |
| aws-cdk-lib | 2.244.0 | latest |
| constructs | bumped | latest |
| jest / @types/jest | bumped | latest |
| jsdom | bumped | 29.0.1 |

---

## Test Fixes

- Removed stale `AgentCoreMemorySessionManager` mock patch from session factory tests — the previous CodeQL commit correctly removed the unused import, but the test was still patching it at the old module path
- Updated shared view page spec with expanded test coverage (254 lines rewritten)
- Updated share export tests to match the new `create_message` API

---

## Deployment Notes

This release includes new backend endpoints and frontend pages but no new infrastructure resources (no new DynamoDB tables or S3 buckets). All changes are backward-compatible.

- **Backend:** Restart App API and Inference API containers to pick up document deletion, displayText, cost dashboard, and dependency upgrades
- **Frontend:** Rebuild and deploy to pick up Analog.js testing migration, displayText rendering, cost dashboard page, and `picomatch` security patch
- **Infrastructure:** Run `npm install` to pick up `picomatch` and `diff` patches in lockfile. Redeploy if using fine-tuning to pick up the default quota hours config.
- **RAG Ingestion:** Redeploy to pick up the Lambda image digest fix and shared embeddings module

---

# Release Notes — v1.0.0-beta.19

**Release Date:** March 25, 2026
**Previous Release:** v1.0.0-beta.18 (March 24, 2026)

---

## Highlights

This release introduces **Conversation Sharing** — a full-stack feature that lets users share point-in-time snapshots of conversations via URL, with public or email-restricted access controls. Alongside that, **session compaction** has been refactored and enabled by default to automatically manage context window size in long conversations, **fine-tuning** gains drag-and-drop dataset uploads and custom HuggingFace model support, and a round of **security hardening** resolves all remaining CodeQL clear-text logging alerts. The frontend production build is now fully optimized (4.96 MB initial, down from 8.85 MB), and PR workflows have been slimmed down to only run build and test steps.

---

## New Feature: Conversation Sharing

Users can now share conversations with others via shareable URLs. Shares are point-in-time snapshots — the shared view captures the conversation as it existed at the moment of sharing, so subsequent messages don't leak into shared links.

### How It Works

- **Share modal** accessible from the session UI lets users create a share with either `public` (anyone with the link) or `specific` (restricted to a list of email addresses) access
- **Manage shares dialog** on the session management page shows all active shares with options to update access levels or revoke
- **Read-only shared view** at `/shared/:shareId` renders the conversation with full markdown formatting, no authentication required for public shares
- **Export support** for downloading shared conversations

### Backend

Three new API routers handle the sharing lifecycle:

- `POST /conversations/{session_id}/share` — Create a share snapshot
- `GET /conversations/{session_id}/shares` — List shares for a session
- `PUT /shares/{share_id}` — Update access level or allowed emails
- `DELETE /shares/{share_id}` — Revoke a share
- `GET /shares/{share_id}/export` — Export shared conversation
- `GET /shared/{share_id}` — Public read-only retrieval

### Infrastructure

A new `shared-conversations` DynamoDB table is provisioned in the Infrastructure stack with two GSIs:

- `SessionShareIndex` — Lookup shares by original session ID
- `OwnerShareIndex` — List shares by owner, sorted by creation time

The table name and ARN are exported via SSM parameters and imported by the App API stack, which grants full CRUD permissions to the Fargate task role.

### Test Coverage

1,300+ lines of new tests across three test files covering share CRUD operations, access control enforcement, export functionality, and property validation.

---

## Session Compaction — Enabled by Default

The session compaction system has been refactored and is now **enabled by default** for all conversations. Compaction automatically manages context window size by summarizing older turns when the token count exceeds the threshold, keeping conversations responsive without manual intervention.

- **Default configuration:** enabled, 100K token threshold, 3 protected recent turns, 500-char max tool content length
- **Turn-based session manager** rewritten with cleaner separation of concerns (870-line net reduction)
- **Expanded test suite** with 481+ new lines of test coverage for compaction behavior

---

## Fine-Tuning Enhancements

### Drag-and-Drop Dataset Upload

The training job creation page now supports drag-and-drop file upload with visual feedback, replacing the basic file picker. Upload instructions have been updated to guide users through dataset formatting requirements.

### Custom HuggingFace Model Support

Users are no longer limited to the preset model list. The training job form now includes a searchable model selector that accepts any valid HuggingFace model identifier. The backend validates and passes custom model IDs through to SageMaker. Frontend tests cover the custom model selection and submission flow.

---

## Security Hardening

### Clear-Text Logging Remediation

All remaining CodeQL clear-text logging alerts have been resolved:

- **`seed_auth_provider`** — Client IDs masked to first 8 characters, Secrets Manager ARNs fully redacted from output
- **`seed_bootstrap_data`** — Full exception objects replaced with error codes in log messages
- **`external_mcp_client`** — Server URLs removed from logs, MCP client configuration logging downgraded from info to debug
- **`oauth_tool_service`** — Decrypted tokens isolated into `_try_get_token()` to prevent taint propagation, lazy log formatting applied
- **`config.ts`** — AWS account IDs and CORS origins removed from CDK config log output

### OAuth Redirect Validation

The OAuth callback endpoint now validates redirect URLs to prevent open redirect vulnerabilities.

### Workflow Permissions

All 13 GitHub Actions workflows now declare explicit `permissions: contents: read`, implementing the principle of least privilege instead of relying on default token permissions.

---

## Frontend Production Optimization

The Angular production build is now fully optimized:

- Removed `optimization: false` override from base build options that was blocking the production configuration
- Production config now enables full optimization, disables source maps, and extracts licenses
- `anyComponentStyle` budget increased from 4 kB to 200 kB to accommodate Tailwind CSS
- **Result:** 4.96 MB initial bundle (871 KB gzipped), down from 8.85 MB unoptimized
- `BUILD_CONFIG` is now branch-aware: `main` → production, `develop` → development, manual dispatch → user input

### Google Fonts Fix

Google Fonts `@import` statements moved from component CSS to `index.html` `<link>` tags, fixing a CI build failure where the CSS optimizer couldn't resolve external font URLs.

---

## CI/CD Improvements

### Lighter PR Workflows

Pull request workflow runs have been significantly trimmed across all 7 deployment workflows. PRs now only run:

- Dependency installation and caching
- Stack dependency validation
- CDK TypeScript compilation (catches build errors)
- Python tests (app-api, inference-api)
- Frontend tests (Vitest)

Skipped on PRs: Docker image builds, Docker image tests, CDK synthesis, CDK validation, ECR push, and deployment. This reduces PR CI time and eliminates the need for AWS credentials on pull requests.

---

## Bug Fixes

- **Bedrock prompt caching** — Caching configuration commented out in model config due to current Bedrock limitations. Tests updated to reflect the change.

---

## Deployment Notes

This release adds a new DynamoDB table (`shared-conversations`) to the Infrastructure stack. Deploy the Infrastructure stack first, then the App API stack. If deploying all stacks simultaneously, the App API deployment may fail on first run due to the SSM parameter dependency — just rerun it after Infrastructure completes.

---
# Release Notes — v1.0.0-beta.18

**Release Date:** March 24, 2026
**Previous Release:** v1.0.0-beta.17 (March 23, 2026)

---

## Highlights

This release is a **supply chain security hardening** release. Every dependency across all three ecosystems (Python, npm, GitHub Actions) has been pinned to exact versions, all GitHub Actions are SHA-pinned, CI runners are locked to `ubuntu-24.04`, Dockerfile `apt`/`dnf` packages are version-pinned, and a new 11-file property-based test suite enforces these invariants going forward. Alongside the hardening, the release adds **CodeQL Advanced security scanning**, a **flexible nightly track system** that replaces the monolithic nightly pipeline, and migrates **RAG resources out of the App API stack** into the dedicated RAG Ingestion stack.

---

## ⚠️ Deployment Note — RAG Data Loss on Existing Deployments

This release removes the assistants documents S3 bucket (`assistants-documents`), S3 Vector Bucket (`assistants-vector-store-v1`), and Vector Index (`assistants-vector-index-v1`) from `AppApiStack`. These resources are now created in `RagIngestionStack` under new names (`rag-vector-store-v1`, etc.). Because CloudFormation tracks resources by logical ID within a stack, deploying this release will cause CDK to delete the old resources from the App API stack. Any existing assistant documents and vector embeddings stored in those buckets will be lost.

If your deployment has data in these resources, you should manually back up or migrate the contents before deploying. If `CDK_RETAIN_DATA_ON_DELETE` is `true` in your environment, the removal policy may be set to `RETAIN`, which would orphan the resources instead of deleting them — but you should verify this against your configuration before relying on it.

---

## Supply Chain Security Hardening

A comprehensive security audit identified 17 findings across GitHub Actions, dependency manifests, Dockerfiles, and install scripts. This release addresses all of them.

### GitHub Actions SHA Pinning

All third-party GitHub Actions are now pinned to specific commit SHAs with version comments (e.g., `actions/checkout@de0fac2e...  # v6.0.2`). This prevents tag-rewriting supply chain attacks where a compromised action could inject malicious code into CI runs.

### Runner Pinning

All workflow jobs now use `ubuntu-24.04` instead of `ubuntu-latest`, ensuring consistent and reproducible build environments that won't silently change behavior when GitHub rolls forward the `latest` tag.

### Exact Dependency Pinning

All three ecosystems have been migrated from range specifiers (`>=`, `^`, `~`) to exact version pins:

- **Python** (`pyproject.toml`): Every dependency uses `==` pins (e.g., `fastapi==0.135.2`, `boto3==1.42.73`, `strands-agents==1.32.0`)
- **npm frontend** (`package.json`): All `^` prefixes removed, exact versions throughout (e.g., `@angular/core` `21.2.5`, `tailwindcss` `4.2.1`)
- **npm infrastructure** (`package.json`): Same treatment (e.g., `aws-cdk-lib` `2.244.0`, `aws-cdk` `2.1113.0`)

### Dockerfile Package Pinning

All `apt-get install` and `dnf install` commands now specify exact package versions:

- App API and Inference API Dockerfiles: `gcc=4:14.2.0-1`, `g++=4:14.2.0-1`, `curl=8.14.1-2+deb13u2`
- RAG Ingestion Dockerfile: All 9 `dnf` packages pinned (gcc, make, mesa-libGL, glib2, tar, gzip, ca-certificates, unzip)

### Script Hardening

All deployment and install scripts now use `npm ci` exclusively (no `npm install` fallback), ensuring lockfile-driven deterministic installs across all environments.

### Artifact Retention Policy

A new `.github/ARTIFACT_RETENTION.md` defines tiered retention periods: Docker tarballs and CDK build artifacts at 1 day, synthesized templates and test results at 7 days, deployment outputs and Trivy scan reports at 30 days. All workflow `retention-days` values have been aligned to this policy.

### Supply Chain Test Suite

A new `backend/tests/supply_chain/` directory contains 11 property-based test files that validate security invariants:

- Action SHA pinning, runner version pinning, dependency exact pinning
- Dockerfile package pinning, artifact retention consistency
- Concurrency configuration, secret scoping, script hardening
- Dependabot configuration, documentation presence

These tests run as part of the standard `pytest` suite and will catch regressions if anyone reintroduces range specifiers or unpinned actions.

---

## CodeQL Advanced Security Scanning

A new `codeql.yml` workflow provides static analysis across three languages: Python, TypeScript, and GitHub Actions. It uses the `security-and-quality` query suite for broad vulnerability and code quality coverage, plus the `github-actions` threat model for full Actions taint tracking (18 queries covering code injection, artifact poisoning, cache poisoning, and secret exposure).

The workflow runs on push and PR to `develop`, plus a weekly scheduled scan to catch new CVEs even when code hasn't changed. A custom `codeql-config.yml` excludes vendored, generated, test, and build artifact paths to keep scan times reasonable. The first scan already surfaced unused imports and variables in the supply chain test suite, which have been cleaned up in this release.

---

## Flexible Nightly Track Selection

The monolithic nightly pipeline has been replaced with a composable track-based system. Instead of a single `NIGHTLY_ENABLED` boolean, the workflow now reads a `NIGHTLY_TRACKS` variable (or `workflow_dispatch` input) containing comma-separated track tokens:

- `test-backend-<branch>` / `test-frontend-<branch>` — Run tests against any branch
- `deploy-<branch>` — Deploy full stack from any branch
- `merge-validation:<base>:<overlay>` — Deploy base, then overlay (simulates merge)
- `scan-images-<branch>` — Scan Docker images for vulnerabilities
- `all` — Run everything with default branches

A new `resolve-tracks` job parses the tokens into boolean flags and branch refs consumed by downstream jobs. The deploy pipeline is extracted into a reusable `nightly-deploy-pipeline.yml` called up to 3 times (deploy track, MV base, MV overlay), eliminating all duplication. Fork safety is preserved — if `NIGHTLY_TRACKS` is empty, nothing runs.

---

## RAG Resources Migration

RAG resources (assistants documents bucket, S3 Vector Bucket, Vector Index) have been removed from `AppApiStack` and are now exclusively managed by `RagIngestionStack`. The App API stack imports these resources via SSM parameters, improving separation of concerns and eliminating cross-stack resource ownership issues.

The vector store IAM permissions in the App API task role now reference the RAG vector bucket imported from SSM (`/${projectPrefix}/rag/vector-bucket-name`) instead of a locally-created bucket, with a named SID (`RagVectorStoreAccess`) for better auditability.

---

## Embeddings Refactor

Core embedding and vector store operations have been extracted from the ingestion pipeline into a new shared module at `apis.shared.embeddings`. The functions `generate_embeddings`, `store_embeddings_in_s3`, `search_assistant_knowledgebase`, and `delete_vectors_for_document` now live in `apis.shared.embeddings.bedrock_embeddings`, with the ingestion-specific module re-exporting them for backward compatibility.

A new `skip_token_validation` parameter on `generate_embeddings` allows callers to bypass tiktoken-based token validation for short inputs in environments where tiktoken is unavailable (e.g., search Lambda functions). The ingestion pipeline retains its own token validation and chunk-splitting logic.

---

## Dependabot Configuration

A new `.github/dependabot.yml` monitors all four ecosystems (pip, frontend npm, infrastructure npm, GitHub Actions) on a weekly Monday 9 AM Mountain Time schedule. Minor and patch updates are grouped to reduce PR noise (Angular updates grouped separately from other frontend deps, AWS CDK grouped separately from other infrastructure deps). All PRs target the `develop` branch with ecosystem-specific labels.

---

## CI/CD Improvements

- **AWS credentials action upgraded** to `v6.0.0` with SHA pinning, plus a new sanitization step that replaces illegal characters in OIDC role session names and truncates to the 64-character AWS limit
- **Explicit OIDC permissions** added to nightly deploy, MV base, and MV overlay jobs (`id-token: write`, `contents: read`)
- **SageMaker conditional gating** — synth job now outputs an `enabled` flag based on `CDK_FINE_TUNING_ENABLED`; test and deploy jobs skip when fine-tuning is disabled
- **Node.js 24 action warnings** fixed after SHA-pinning reintroduced older action references

---

## Dependency Upgrades

| Component | From | To |
|---|---|---|
| FastAPI | 0.116.1 | 0.135.2 |
| Starlette | 0.47.3 | 1.0.0 |
| strands-agents | 1.27.0+ | 1.32.0 |
| strands-agents-tools | 0.2.20 | 0.2.23 |
| boto3 | 1.40.1+ | 1.42.73 |
| bedrock-agentcore | latest | 1.4.7 |
| Angular packages | 21.0.x | 21.2.5 |
| @angular/cdk | 21.0.3 | 21.2.3 |
| Tailwind CSS | 4.1.12+ | 4.2.1 |
| aws-cdk-lib | 2.235.1 | 2.244.0 |
| aws-cdk (CLI) | 2.1033.0 | 2.1113.0 |
| DOMPurify | 3.3.1 | 3.3.3 |
| undici | 7.22.0 | 7.24.5 |
| hono | 4.12.2 | 4.12.9 |
| katex | 0.16.25 | 0.16.33 |
| mermaid | 11.12.1 | 11.12.3 |
| Vitest | 4.0.8 | 4.0.18 |
| mypy target | py3.9 | py3.10 |

---

## Bug Fixes

- **Fine-tuning dashboard** — Removed an incorrect "retention" label from the inference job display on the SageMaker fine-tuning dashboard.

---

## Documentation & Developer Experience

- Added `CONTRIBUTING.md` with prerequisites, clone/install instructions, environment configuration, testing commands, and contribution workflow
- Supply chain hardening spec (requirements, design, tasks) added under `.kiro/specs/supply-chain-hardening/`

---


---

# Release Notes — v1.0.0-beta.17

**Release Date:** March 23, 2026
**Previous Release:** v1.0.0-beta.16 (March 20, 2026)

---

## Highlights

This release delivers three major improvements: a **centralized Settings experience** that consolidates scattered user preferences into dedicated pages backed by a new DynamoDB table, a **pip-to-uv migration** that modernizes the entire Python build pipeline with hardened Docker images, and **runtime environment refresh** so AgentCore containers always pick up the latest SSM parameter values on every deploy instead of carrying forward stale configuration.

---

## Centralized User Settings

The user dropdown menu has been slimmed down to just email, admin link, settings, and logout. All user-facing features that were previously scattered across the dropdown and standalone pages have been consolidated into a `/settings/*` route hierarchy with dedicated pages:

- **Profile** — Read-only user info display with a link to My Files
- **Appearance** — Theme chooser (persisted to localStorage) with placeholders for density and font size
- **Chat Preferences** — Default model selector backed by a new User Settings API (`GET/PUT /users/me/settings`), show-token-count toggle, and links to Manage Conversations and Memories
- **Connections** — Full OAuth connect/disconnect flow via a new `ConnectionsService`
- **API Keys** — Migrated from the standalone `/api-keys` page with loading states
- **Usage** — Migrated from the standalone `/costs` dashboard with a month picker for historical data

### Backend

A new `user-settings` DynamoDB table and repository store per-user preferences (starting with `defaultModelId`). The table is provisioned in the Infrastructure stack with IAM permissions granted to both the App API Fargate tasks and Inference API runtime roles. Graceful degradation is built in — if the table doesn't exist yet, the API returns defaults without errors.

### Removed

The standalone Notifications and Privacy settings pages were removed as unnecessary.

---

## pip → uv Migration

The entire Python toolchain has been migrated from pip to [uv](https://docs.astral.sh/uv/), affecting Docker builds, CI pipelines, and local development workflows.

### Docker Security Hardening

- All base images pinned to `@sha256` digests (Python 3.13-slim, Lambda Python 3.12)
- Non-root `USER` directive added to the App API Dockerfile
- Rust toolchain installed via `COPY --from=rust:1.87-slim` (pinned digest) instead of `curl | sh`
- Torch pinned to exact version (`2.10.0`) in RAG ingestion with `--require-hashes` install from a generated `requirements.lock`
- `curl` removed from builder stages

### CI/CD

- All three Dockerfiles (app-api, inference-api, rag-ingestion) rewritten for uv
- CI install and test scripts updated for both app-api and inference-api
- Workflow caching switched to uv cache paths
- `backend/uv.lock` added to workflow path triggers
- `sync-version.sh` now handles `uv.lock` regeneration with PEP 440 version conversion

### New Release Workflow

A standalone `release.yml` workflow triggers on push to main, creating annotated git tags and GitHub Releases from `RELEASE_NOTES.md`. Pre-release versions (alpha/beta/rc/dev) are automatically detected and flagged.

### Dependabot

A new `.github/dependabot.yml` monitors pip, npm, and GitHub Actions dependencies.

---

## Runtime Provisioner: SSM Environment Refresh

Previously, when an AgentCore runtime was updated (e.g., on redeploy), the provisioner Lambda preserved the existing environment variables from the original runtime creation. This meant renamed tables, new SSM parameters, or changed values were never picked up.

Now, `update_runtime()` re-fetches all environment variables from SSM on every update. A fallback to existing values is included if the SSM refresh fails, maintaining stability. The runtime-updater Lambda also gained a `get_fresh_environment_variables()` function for consistent handling.

---

## Configurable Memory Retrieval Thresholds

AgentCore Memory retrieval is now tunable via two new environment variables:

- `AGENTCORE_MEMORY_RELEVANCE_SCORE` — Minimum relevance score for retrieved memories (default raised from 0.3–0.5 to 0.7)
- `AGENTCORE_MEMORY_TOP_K` — Maximum number of memories to retrieve

All memory-related environment variables have been renamed from `COMPACTION_*` to `AGENTCORE_MEMORY_COMPACTION_*` for consistent naming.

---

## Assistant UX Improvements

The assistant experience in the chat interface received several polish updates:

- **Action dropdown** on the assistant indicator with options to start a new session, edit the assistant, or share it
- **Share dialog** on the assistant form page for sharing assistants with other users
- **Skeleton loading indicators** replace blank states while the assistant and chat input are loading
- **Improved greeting visibility** — the assistant greeting now shows/hides properly based on loading state
- **Sidenav updates** — the new session button and assistant navigation link are now accessible from the sidebar
- **Responsive card layout** fix for the assistant list page

---

## SageMaker Fine-Tuning Fixes

- **Job name scoping** — Training and transform job names are now prefixed with `PROJECT_PREFIX` to match the IAM policy's `${projectPrefix}-*` resource constraint. Previously, jobs used `ft-` and `inf-` prefixes which caused `AccessDeniedException` on `CreateTrainingJob`.
- **Missing IAM actions** — Added `sagemaker:CreateModel` and `sagemaker:DeleteModel` actions plus the model resource ARN to the IAM policy for transform job support.
- **Log access** — Added `logs:DescribeLogStreams` to the IAM policy so the fine-tuning dashboard can display SageMaker training logs.
- **CDK toggle** — Added `CDK_FINE_TUNING_ENABLED` environment variable to the app-api CI workflow for conditional stack deployment.

---

## Bug Fixes

- **User settings API trailing slashes** — Removed trailing slashes from the `/users/me/settings` routes that caused 307 redirects on some HTTP clients.
- **Assistant list card layout** — Fixed responsive grid breakpoints on the assistant list page so cards don't overflow on narrow viewports.

---

## Documentation & Developer Experience

- Updated `CLAUDE.md` with revised coding standards, testing guidelines, and file creation rules
- README logo and header formatting refreshed for better visibility and alignment

---


---

# Release Notes — v1.0.0-beta.16

**Release Date:** March 20, 2026
**Previous Release:** v1.0.0-beta.15 (March 20, 2026)

---

## Hotfix: Runtime Provisioner SSM Path

The runtime provisioner Lambda was still referencing the old `/file-upload/table-name` SSM parameter path for the user files DynamoDB table. This caused `AccessDeniedException` on `dynamodb:GetItem` because the AgentCore runtime container received the old table name (`user-files`) while the IAM policy was scoped to the new table (`user-file-uploads`). Updated to `/user-file-uploads/table-name` to match the Infrastructure stack's SSM exports.

---

---

# Release Notes — v1.0.0-beta.15

**Release Date:** March 20, 2026
**Previous Release:** v1.0.0-beta.8 (March 16, 2026)

---

## Highlights

This release introduces the **SageMaker Fine-Tuning** stack — a complete model training and inference platform built on Amazon SageMaker, deployable as an optional CDK stack. Beyond that, the release delivers **security hardening**, **deployment reliability**, and **platform modernization**: RBAC model access enforcement is now applied at the inference layer, the nightly CI/CD pipeline gains a full merge-validation track to catch integration issues before release, and the entire stack has been upgraded to current runtime versions (Python 3.13, Angular 21.2, Node.js 24 Actions, CDK 2.1112).

---

## ⚠️ Deployment Note

Merging this release will trigger all stack workflows simultaneously. File upload resources (S3 bucket, DynamoDB table, SSM parameters) were moved into the Infrastructure stack, so the App API and Inference API deployments may fail if Infrastructure hasn't finished yet. This is expected — just rerun the failed workflows after the Infrastructure deployment completes.

---

## New Feature: SageMaker Fine-Tuning

A complete model fine-tuning platform has been added, allowing users with admin-granted access to train and run inference on open-source models directly from the UI.

- New `SageMakerFineTuningStack` CDK stack with DynamoDB tables, S3 storage, and IAM roles for SageMaker training/inference
- Backend API with full CRUD for training jobs, inference jobs, and admin access management (`/fine-tuning/` routes)
- SageMaker integration for launching training jobs on models like BERT, RoBERTa, and GPT-2 with configurable hyperparameters (epochs, batch size, learning rate, train/test split)
- Batch inference support on trained models with real-time progress tracking
- Frontend dashboard with job creation wizards, detail pages, status badges, quota cards, and dataset upload via presigned S3 URLs
- Admin access control page for granting/revoking fine-tuning permissions per user
- Automatic 30-day artifact retention with lifecycle policies
- Dedicated CI/CD workflow (`sagemaker-fine-tuning.yml`) with build, synth, test, and deploy scripts
- EC2 networking permissions for VPC-based training jobs
- Elapsed time display and polling for active jobs
- Comprehensive test suite (admin routes, user routes, repositories, SageMaker service, training/inference scripts)

---

## Community Contribution 🎉

This release includes our first outside contribution! Thanks to [@magicfoodhand](https://github.com/magicfoodhand) for **Session List Grouping Enhancements** (#43) — the session sidebar now groups conversations by date range (Today, Yesterday, Previous 7 Days, etc.) and supports inline session renaming. A great UX improvement.

---

## Bug Fixes

- **RBAC model access not enforced on Inference API** (#31, #47) — Role-based model access was only checked on the App API side, allowing the Inference API's Converse and Invocations endpoints to bypass model-level RBAC. Both endpoints now call `can_access_model()` and reject unauthorized requests with HTTP 403 before any Bedrock invocation occurs. Includes 1,500+ lines of new test coverage.
- **Deprecated `datetime.utcnow()` replaced** — All backend modules (quota recorder, admin models, user service, file service, tools, document ingestion) now use timezone-aware `datetime.now(timezone.utc)`, resolving Python 3.12+ deprecation warnings.
- **Cross-stack SSM deployment failure properly fixed** — File upload resources (S3 bucket, DynamoDB table, SSM parameters) have been relocated from `AppApiStack` to `InfrastructureStack`, eliminating the cross-stack dependency that caused first-time deployment failures. The beta.8 hotfix (hardcoded ARN construction) was a temporary workaround; this is the permanent solution.
- **Dependency conflict resolved** — Pillow was temporarily removed then restored alongside numpy to resolve a packaging conflict with `strands-agents-tools`.

---

## Infrastructure & Configuration

### File Upload Resources Relocated to Infrastructure Stack
File upload S3 bucket and DynamoDB table have been moved from `AppApiStack` to `InfrastructureStack` to eliminate the cross-stack dependency between Inference API (tier 2) and App API (tier 3). Unfortunately, the path of least resistance was to recreate these resources with new names, so be aware that some data loss may occur when updating an existing deployment. SSM parameter paths have been renamed from `/file-upload/` to `/user-file-uploads/` for consistency. 

### Auto-Derived CORS Origins
Deployments no longer require explicit `CDK_CORS_ORIGINS`. If only `CDK_DOMAIN_NAME` is set, CORS origins are automatically derived as `https://<domain>`. This simplifies initial setup and reduces configuration errors.

### Unified Removal Policies
S3 buckets and Secrets Manager secrets across all stacks (`AppApiStack`, `InfrastructureStack`, `RagIngestionStack`) now use config-driven removal policies via `getRemovalPolicy(config)` and `getAutoDeleteObjects(config)` instead of hardcoded `RETAIN`. This enables clean teardown in non-production environments.

### AWS Account in Resource Naming
`getResourceName()` calls for S3 buckets now include `config.awsAccount`, ensuring unique and consistent resource names across multi-account deployments. Be aware of potential data loss when updating existing deployments as the default bucket naming scheme has changed. Each stack will now suffix the account number to prevent s3 name collisions.

---

## Platform Upgrades

| Component | From | To |
|---|---|---|
| Python runtime | 3.11 | 3.13 |
| FastAPI | 0.116.1 | 0.135.1 |
| Uvicorn | 0.35.0 | 0.42.0 |
| strands-agents-tools | 0.2.20 | 0.2.22 |
| Angular packages | 21.0.x | 21.2.x |
| Algolia client packages | 5.46.2 | 5.48.1 |
| AWS CDK | 2.1033.0 | 2.1112.0 |
| @types/jest | — | ^30.0.0 |
| jest | — | ^30.3.0 |
| Starlette | — | >=0.49.1 (new explicit dep) |
| cryptography | — | >=46.0.5 (new explicit dep) |

---

## CI/CD & DevOps

### Nightly Pipeline Improvements
A new merge-validation track deploys `main` branch infrastructure first, then deploys `develop` branch on top — simulating the real merge scenario. This catches integration issues between branches before they reach production. The track includes full stack deployment (infrastructure → RAG ingestion → inference API → app API → frontend) with automatic teardown. Nightlies also no longer rebuild Docker images; a new `promote-ecr-image.sh` script copies pre-built images from the develop ECR repository to the target environment, cutting pipeline time and ensuring image parity with what was tested on develop.

### Stack Dependency Validation
All GitHub workflows now include a `check-stack-dependencies` gate job that validates CDK stack dependencies before any build or deploy step runs. A new `test-stack-dependencies.sh` script powers this check.

### GitHub Actions Node.js 24 Migration
All GitHub Actions have been upgraded to Node.js 24-compatible versions:
- `actions/checkout` v4 → v5
- `actions/cache` v4 → v5
- `actions/upload-artifact` / `download-artifact` v4 → v5 (then v7)
- `aws-actions/configure-aws-credentials` v4 → v6
- `docker/setup-buildx-action` v3 → v4
- `docker/build-push-action` v6 → v7

### Additional CI Improvements
- Fork guard prevents accidental nightly runs on forked repositories
- Package-lock.json sync validation added to version-check workflow
- Frontend build caching with split build/deploy steps (nightly)
- Centralized pipeline summary table
- Artifact handling switched from cache to upload/download actions
- Retry logic added to smoke test health checks
- S3 Vector Bucket cleanup added to teardown scripts (nightly)
- CloudWatch log group cleanup added to teardown scripts (nightly)
- Reduced CI log verbosity across all workflows

---
