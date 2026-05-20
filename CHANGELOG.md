# Changelog

All notable changes to this project are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For narrative release notes written for operators and product owners, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [1.0.0-beta.27] - 2026-05-20

Two new user-facing surfaces ship behind feature flags: **Artifacts** (a separate `artifacts.{domain}` CloudFront origin + Lambda renderer that serves agent-authored HTML/Markdown into a sandboxed iframe panel docked beside chat) and **MCP Apps host-renderer** (a `mcp-sandbox.{domain}` proxy origin + `<mcp-app-frame>` postMessage bridge that frames `ui://` resources returned by MCP tools, with app-initiated `tools/call` proxying and `ui/update-model-context` mutations). Admin gets a persistent sidebar shell with grouped nav, admin-managed user-menu links, and compact expandable list redesigns for models and tools. Recoverable max-tokens truncation with a Continue affordance, model-aware adaptive thinking for Opus 4.7, a `/ping` reaper-fix on the inference runtime, and `bedrock-agentcore` 1.6.4 → 1.9.1 / `strands-agents` 1.39.0 → 1.40.0 / `boto3` 1.42.96 → 1.43.9 bumps round it out.

### 🚀 Added

- **Artifacts stack** — new `ArtifactsStack` provisions a private S3 content bucket, DynamoDB heads + version log, CloudFront at `artifacts.{domain}` (reuses existing `*.{domain}` cert), Route53 alias, Python render Lambda, and 5 SSM exports. Gated end-to-end on `CDK_ARTIFACTS_ENABLED` (#306)
- Artifacts render Lambda — stdlib HS256 token verification (alg-pinned, constant-time compare, iss/aud/exp validation, ≤10min lifetime), DynamoDB version lookup, S3 content fetch, strict CSP / security-header passthrough. 38-test suite including moto-based integration (#309)
- `POST /artifacts/{artifact_id}/render-token` (app-api) — mints the short-lived HS256 JWT the render Lambda verifies. Validates ownership via session and confirms the version exists in DDB so callers get a clean 404 instead of an iframe-rendered error page (#310)
- `create_artifact` / `update_artifact` Strands tools — write a standalone HTML/Markdown document to S3 and frozen DDB version + HEAD rows. Versions are immutable; update writes a new version and re-points HEAD under an optimistic version-match condition. Wired into inference-api chat routes via `_build_artifact_tools`, gated on `enabled_tools` (#311)
- `artifact` SSE event — emitted post-`message_stop` next to `compaction`, gated on a create/update tool call that turn and filtered via the SessionIndex GSI (no fragile tool-text parsing). `GET /artifacts?session_id=` on app-api for refresh-survival hydration (#312)
- SPA artifact panel — `ArtifactStateService` signal store, `ArtifactHttpService`, parser support, session-level inline card strip, slide-over panel that re-mints a short-lived render token per open and iframes the artifact sandboxed (`allow-scripts`, no `allow-same-origin`, no-referrer). Docks beside chat via a draggable, keyboard-accessible resize handle (#312, #319)
- Artifact UX iterations — inline tool-rail streaming during creation (#316), inline cards anchored to their producing message (#317), Markdown content type (#318), full-width inline card + download button (#321), preview/code toggle with syntax-highlighted source (#322), per-version history cards + panel version picker (#324), auto-open panel on create with skeleton loader and latest-version-on-update (#325)
- Artifacts: configurable extra CSP `frame-ancestors` for embedding artifacts in external sites (#314)
- Artifacts: register `create_artifact` / `update_artifact` as default public tools in the seed script (#334)
- **MCP Apps sandbox-proxy origin** — new `McpSandboxStack` stands up `mcp-sandbox.{domain}` (S3 + CloudFront + ACM + Route53) serving a static `proxy.html` shell. CloudFront response-headers policy locks CSP `frame-ancestors` to the SPA origin. Tier-1 (parallel-safe with Artifacts/RAG/Gateway/Fine-Tuning); inert until consumed (#343)
- MCP Apps: `initialize` advertises the UI extension on the MCP client, and app-only tools are filtered from the agent's tool list (#342)
- MCP Apps: `ui_resource` SSE event — emitted after a correlated `tool_result` when the tool declared a `ui://` resource. Payload `{type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin}`. HTML fetched server-side via `resources/read` and inlined; `sandboxOrigin` is the proxy.html origin the SPA frames it in (#344, #345)
- MCP Apps: `<mcp-app-frame>` web component + postMessage bridge — blob-iframe-mounts the inlined HTML inside the sandbox proxy with a strict per-resource CSP and a typed postMessage protocol (#346)
- MCP Apps: app-initiated `tools/call` proxying + event broker — Apps can request tool invocations on their behalf with consent-gated dispatch (#347)
- MCP Apps: `ui/message`, `ui/update-model-context`, frontend consent + reload persistence — Apps can post messages to the host, mutate the next-turn model context, and persist user consent decisions across reloads (#348)
- MCP Apps: dynamic per-resource CSP for the sandbox proxy — proxy.html now reads `?csp=` and applies the per-`ui://` resource policy in addition to the outer CloudFront defaults (#355)
- MCP Apps: `AGENTCORE_MCP_APPS_HOST_ENABLED` flipped default → `True`. Inference-api conditionally consumes the `mcp-sandbox` origin SSM into `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` when `CDK_MCP_SANDBOX_ENABLED=true` (#349)
- `effort` inference parameter — canonical param mapped to `output_config.effort` via `additional_request_fields`, plus generic `allowed` enum on `ModelParamSpec`. Surfaced through the admin model form and the user-facing chat settings panel; server-side allowed-set gating in `_merge_inference_params`. Seeded on Sonnet 4.6 (#331)
- Recoverable `max_tokens` truncation — backend classifies `MaxTokensReachedException` specifically and emits a `max_tokens`-coded recoverable `stream_error`; Continue re-enters the agent loop with an empty-list prompt so the model resumes the truncated assistant message (assistant-prefill) instead of re-answering from scratch. `lastTurnContinuable` flows through `SessionMetadataResponse` so Continue reappears after reload. Compact inline "Response length limit reached" notice in the SPA (#328)
- Admin-managed user-menu links — new admin domain (`/admin/user-menu-links`) lets admins curate links shown in the SPA user menu without code changes. Each link is either an external URL (new tab, `rel="noopener noreferrer"`) or an in-app modal with admin-authored Markdown. `UserMenuLinksTable` + public enabled-only read at `/user-menu-links` (#298)
- Admin persistent sidebar shell — replaces the 15-card admin grid with a grouped sidebar nav (Usage & Spend, AI Configuration, Identity & Access, Customization). `/admin` redirects to `/admin/costs`. Quotas consolidated into a single tabbed page (Tiers / Assignments / Overrides / Inspector / Events) and Fine-Tuning consolidated at `/admin/fine-tuning` — URLs unchanged for back-compat (#300)
- Chat code blocks have a copy-to-clipboard button (#299)
- Chat message input autofocuses on session load and on session switch (#333)
- Sidebar session list is denser, with a skeleton placeholder during load and entry animation on new sessions (#301)
- Backup tool + architecture-rules document for the Copilot CLI; new `backup-data` GitHub workflow (#361)

### ✨ Improved

- **Tool-result rendering registry** — replaced the implicit text/JSON/image switch in `ToolUseComponent` with a signal-backed `ToolRendererRegistryService` keyed by tool name. Default renderer reproduces the prior markup verbatim; `calculator`, `fetch_url_content`, and `create_visualization` migrated as proof points. Enables MCP Apps `ui_resource` and per-tool custom cards (#339)
- Model-aware adaptive thinking — `_shape_thinking_value` is now model-aware: Opus 4.6/4.7, Sonnet 4.6, and Mythos emit `{type:"adaptive", display:"summarized"}` (display keeps the reasoning trace from going blank since Opus 4.7 defaults it to `"omitted"`); older models keep the legacy `{type:"enabled", budget_tokens:N}` shape. Opus 4.7 rejects `thinking.type="enabled"` with a 400 — this fix unblocks it (#331)
- Admin model views (Bedrock, Gemini, OpenAI, manage-models) redesigned as compact expandable rows with a slim inline filter toolbar; inline enable/disable toggle on manage-models so status changes no longer require the edit form. Border radius standardized on `rounded-2xl` (#332)
- Admin tools catalog and create/edit form aligned to the same compact `rounded-2xl` redesign — expandable list rows with inline detail panel, flattened form using shared list-page token set. Purely visual (#335)
- Admin shell widened; sidebar label wrapping fixed (#305)
- 24 loading spinners across admin/settings/fine-tuning/auth pages were rendering as a uniform gray ring in dark mode — fixed Tailwind v4 dark-shorthand ordering (drive-by in #300)

### 🐛 Fixed

- `bedrock-agentcore` runtime `/ping` was missing the integer `time_of_last_update` field, so AgentCore's idle reaper would silently reap the microVM at `idleRuntimeSessionTimeout` mid-stream regardless of reported status (bedrock-agentcore-sdk-python#471). `/ping` now returns a fresh timestamp on every call and corrects status casing to match `PingStatus` (#338)
- `max_tokens` inference param could be passed as a string, breaking Bedrock validation; coerced to `int` and capped at the model's ceiling. Same int-coercion + thinking/`max_tokens` guard hardening applied across the inference param merge (#329, #330)
- Frontend deploy builds were baking a placeholder version string; the real `VERSION` file value is now stamped at build time (#336)
- Docker base image's pinned `curl` was at an unavailable version blocking dev deploys; bumped to `8.14.1-2+deb13u3` (#327)
- Artifacts Chart.js renders failed under the strict CSP — allowlisted `jsdelivr` and `unpkg` CDNs in the render Lambda's CSP (#326)
- Artifact card z-index leaked across the chat surface; scoped with `isolation:isolate` (#323)
- Admin user-menu-links resource was loading twice on page entry; gated to a single load (#315)
- Admin user-menu-link modal and preview now visually distinguish internal vs. external link types (#303)
- MCP Apps inner iframe needed `allow-same-origin` to match the ext-apps basic-host reference and unblock App rendering (#360, #353, #352)
- MCP Apps sandbox CFN was URL-encoding `?csp=` queries; the proxy now decodes them. Added `x-csp-debug` diagnostic (later removed once verified) (#358, #359)
- MCP Apps RHP/CFN `Comment` fields exceeded the 128-char AWS cap; shortened (#356, #357)
- Bedrock-agentcore 1.43 botocore newly reads `Credentials.account_id` during endpoint construction; on `RefreshableCredentials` (SSO) this forced a `GetRoleCredentials` refresh which moto doesn't implement. Combined with `AWS_PROFILE` leaking from `backend/src/.env` via `load_dotenv(override=True)`, this red the suite order-dependently. Added per-test autouse scrub fixtures for `AWS_PROFILE` and the `DYNAMODB_*` / `COGNITO_*` config families (#337)

### ⚠️ Changed

- **`AGENTCORE_MCP_APPS_HOST_ENABLED` now defaults `True`.** Set `=false` to opt an environment out (#349)
- All `Bearer`-only auth dependencies removed from `apis/app_api/` post-BFF migration. SPA-facing routes use `Depends(get_current_user_from_session)` (cookie-based) or `Depends(require_admin)`. Exceptions remain `auth/api_keys/` (`X-API-Key`) and `voice/` (voice-ticket cookie) — do not template off these (#297)

### 🔒 Security

- Render-token verifier pins `alg=HS256` (rejects `none` + HS/RS confusion), constant-time signature compare, iss/aud/exp validation with bounded lifetime, non-dict header/payload rejection. The handler never logs the event/query string because the render token is a URL bearer credential (#309)
- Artifact iframes mount with `allow-scripts` only — no `allow-same-origin`, no-referrer. The render token is re-minted per open (#312)
- MCP Apps sandbox proxy enforces dynamic per-`ui://` resource CSP layered over the outer CloudFront `frame-ancestors`-locked policy (#355)

### 📦 Dependencies

- Backend: `bedrock-agentcore` 1.6.4 → 1.9.1, `strands-agents` 1.39.0 → 1.40.0 (+ `strands-agents[bidi]`), `boto3` 1.42.96 → 1.43.9 (+ botocore/s3transfer)

### 🏗️ Infrastructure

- New `ArtifactsStack` — DynamoDB heads + version log, private S3 content bucket, CloudFront at `artifacts.{domain}`, Python render Lambda, Route53 alias, 5 SSM exports. Gated on `CDK_ARTIFACTS_ENABLED`. Tier 1 (parallel-safe). InfrastructureStack publishes `/{prefix}/artifacts/render-token-key-arn`; ArtifactsStack consumes it and publishes bucket/table/origin keys; InferenceApiStack/AppApiStack/FrontendStack consume those when the flag is on (#306)
- New `McpSandboxStack` — `mcp-sandbox.{domain}` (S3 + CloudFront + ACM + Route53) serving `proxy.html`. CSP `frame-ancestors` locked to SPA origin. Tier 1 (parallel-safe). Publishes `/{prefix}/mcp-sandbox/origin` consumed by inference-api into `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` when `CDK_MCP_SANDBOX_ENABLED=true` (#343, #349)
- `stack-dependencies` test (17/17 passing) enforces tier ordering across the new stacks
- Test infra: DynamoDB-table count test now enumerates tables instead of asserting a hard-coded count, so adding tables no longer breaks the suite (#350)

### 🔧 CI/CD

- New `artifacts.yml` workflow (381 lines) — deploys the ArtifactsStack with the standard env-var fanout and synth/deploy gates
- New `mcp-sandbox.yml` workflow (383 lines) — deploys the McpSandboxStack
- New `backup-data.yml` workflow (102 lines) — ad-hoc backup of DynamoDB data
- Artifact env vars (`CDK_ARTIFACTS_ENABLED` and friends) plumbed through every consumer workflow (app-api, inference-api, frontend, infrastructure) (#307)

### 📚 Docs

- `step-04-deploy.md` — new "Register an MCP-Apps-capable MCP server" section (discover → create admin API, local-dev env, the `budget-allocator-server` example) + committed example `ToolCreateRequest` payload. No auto-seed — registration stays an explicit per-env opt-in (#349)
- `step-05-verify.md` — manual e2e dogfood scenario exercising all six MCP Apps Definition-of-Done interactions (resource fetch → iframe render → tool-input push → app-initiated `tools/call` → `ui/update-model-context` → `ui/open-link` consent) (#349)
- `step-02-aws-setup.md` — Artifacts cert-reuse guidance corrected for subdomain primaries; existing `*.{domain}` cert is reused, no new ACM cert required (#308)
- `CLAUDE.md` — `ui_resource` SSE row + deploy-order line updated for the live flag and conditional `mcp-sandbox` SSM consumption (#349)
- `.env.example` — documents `BFF_COOKIE_DATA_KEY_SECRET_ARN` (carry-over from beta.25) (#276)
- New A2A guard: if exposing an A2A server, `AgentCard.capabilities` must include `streaming=True` or clients hang ~40 min (#338)

## [1.0.0-beta.26] - 2026-05-13

Small focused release. Multi-sheet XLSX support for the spreadsheet analysis tool, async refactor of the spreadsheet file-lookup path, user default model preference applied at chat time, nightly E2E pipeline restored, and upstream contribution governance (PRs restricted to collaborators, Dependabot version-update PRs disabled).

### 🚀 Added

- Multi-sheet XLSX support in the `analyze_spreadsheet` tool. Each sheet converts to its own deterministic CSV (`stem.sheetname.csv`) with a primary alias (`stem.csv`) for the first sheet. Defensive caps via env vars `MAX_SHEETS_TO_CONVERT` and `MAX_ROWS_PER_SHEET` prevent latency blowout and context-window exhaustion on pathological workbooks. Skipped/truncated sheets are surfaced to the model with markdown footers documenting per-sheet conversion status
- `_sanitize_sheet_name()` produces filesystem-safe deterministic CSV filenames; `_parse_sheet_inventory()` extracts structured sheet metadata from bootstrap stdout without `eval`-style evaluation; `_safe_int()` for defensive integer parsing; `_format_sheet_note()` for the per-call markdown footer

### ✨ Improved

- `analyze_spreadsheet`, `list_spreadsheets`, `_find_file`, `_get_kb_files`, and `_get_session_files` are now `async def`. Every DynamoDB call is offloaded via `asyncio.to_thread` so the event loop keeps scheduling other coroutines for the full round-trip duration
- `inference_api/chat/routes.py::_build_tabular_inventory` is now `async` and awaits the file-operation calls directly, replacing the nested `asyncio.run` + thread pool executor pattern that could deadlock under concurrent chat load. Closes the regression introduced in #260
- `analyze_tool` code generation stashes the filename as a `_FNAME` variable inside the generated snippet to prevent f-string interpolation conflicts when filenames contain quotes or special characters (`repr()` indirection in `_build_preview_code`)
- `_clean_stderr` now respects the `MAX_ERROR_CHARS` budget strictly, accounting for ellipsis length

### 🐛 Fixed

- User-saved default model preference (`defaultModelId` in user settings) is now applied at chat time when the request doesn't specify a `model_id`. Previously the persisted preference was silently ignored and chat fell back to the hardcoded factory default. RBAC is re-checked on the resolved default to prevent access to permissions that have since been revoked. A missing user-settings table now surfaces as `503` instead of silently dropping the user choice. Fixes #161
- Nightly E2E pipeline failures from cookie/JWT validation against the dynamic CloudFront URL, missing CDK certificate ARN in the nightly job, agent test timeouts on multi-tool turns, and cross-region Bedrock model routing flakes (switched the suite from global to US-region model IDs) (#290)

### 📚 Docs

- `backend/src/.env.example` — BFF cookie encryption documentation updated to reflect the beta.25 shift from direct KMS cookie encryption to Secrets Manager-mediated approach. Documents the new `BFF_COOKIE_DATA_KEY_SECRET_ARN` variable, the SHA-256 cross-task derivation, and the SSM parameter path with example ARN format

### 🔧 CI/CD

- Nightly E2E pipeline restored after multi-attempt fix (#290): CloudFront URL handling, CDK certificate ARN wiring, agent test timeout bumps, US-region Bedrock model IDs, rebase on develop to pick up #248

### 🛡️ Governance

- **CONTRIBUTING.md** documents that pull requests are restricted to approved collaborators (GitHub "Collaborators only" setting). Issues remain open to everyone; maintainers triage and either implement upstream or coordinate next steps with the reporter. Adds collaborator checklist (link tracking issue, single logical change per PR, DCO sign-off, green CI, respect backend import boundaries enforced by `backend/tests/architecture/test_import_boundaries.py`) (#293)
- **`.github/dependabot.yml`** — `open-pull-requests-limit: 0` across all four ecosystems (pip, frontend npm, infrastructure npm, github-actions). Disables scheduled version-update PRs; security updates are unaffected and will still be raised when a CVE is published. Existing groups, labels, schedules retained for easy reversal (#293)

### 🧪 Test Coverage

- `backend/tests/agents/builtin_tools/spreadsheet_analysis/` — 2,800+ lines of new tests across 8 files. Notable: `test_analyze_tool_integration.py` (779 lines, multi-sheet XLSX + CSV workflows end-to-end), `test_sheet_inventory.py` (307 lines, parser robustness against malformed bootstrap output), `test_clean_stderr.py` (202 lines, strict error-char budget), `test_build_preview_code.py` (127 lines, filename escaping), plus `test_helpers.py`, `test_find_file.py`, `test_list_spreadsheets.py`, `test_strip_first_row.py`
- `frontend/ai.client/src/app/session/services/model/model.service.spec.ts` (56 lines) — default-model resolution flow
- `frontend/ai.client/src/app/settings/pages/chat-preferences/chat-preferences-settings.page.spec.ts` (101 lines) — Chat Preferences settings UI

## [1.0.0-beta.25] - 2026-05-11

Production-readiness fix for the BFF Token Handler shipped in beta.24. Fixes three production-breaking bugs introduced by beta.24: event-loop-blocking sync boto3 on every cookie-bearing request, per-process AES-256 keys that can't round-trip cookies across ECS tasks, and an in-process-only refresh lock that races Cognito rotation across replicas. Also ships PDF thumbnails, rich attachment previews, spreadsheet analysis tools, centralized 401 handling, and a `SKIP_AUTH` local-dev bypass.

### 🐛 Fixed

- **Critical (beta.24 regression):** `SessionRefreshMiddleware` ran sync boto3 (DynamoDB + Cognito) on the uvicorn event loop so Angular's ~8-endpoint page-load fan-out produced ~16 serialized blocking AWS calls per user per minute. Observable as ALB 504s, 15.6s p-max `TargetResponseTime` at 0.7% CPU, `/files/quota` outliers reaching ~80s. Every boto3 call in `SessionRepository` and `CognitoRefreshClient.refresh` now offloads via `asyncio.to_thread`; `_resolve_session` is wrapped in a per-session `asyncio.Future` single-flight so N concurrent same-session callers share one loader invocation; `_maybe_slide` dispatches `touch_last_seen` as a detached `asyncio.Task` (with strong reference on the middleware to prevent GC); `_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS` raised 60s → 300s to de-align from the 60s refresh-leeway window (#264)
- **Critical (beta.24 regression):** `CookieCodec` called `kms:GenerateDataKey` on first use per process, so each app-api task minted its own random AES-256 key. Once `desiredCount` went above 1, cookies sealed on Task A failed as `bad seal` on Task B (~50% of requests). Data key is now generated once via Secrets Manager `generateSecretString` (44-char, ~261 bits entropy) encrypted at rest with the existing `BFFCookieSigningKey` CMK; `CookieCodec._ensure_cipher` reads the secret and derives the AES-256 key via SHA-256; `kms:GenerateDataKey` dropped from the runtime task role (#273, #274)
- **Critical (beta.24 regression):** In-process `single_flight` and `get_session_lock` only coalesce same-session callers within one Python process. Under multi-replica, two tasks could each call `cognito-idp:initiate_auth` with the same refresh token; Cognito rotates on the winner and the loser silently logs the user out. New DDB conditional-write lock (`try_acquire_refresh_lock` / `release_refresh_lock` on `BFFSessionsTable`, reusing the existing `dynamodb:UpdateItem` grant) elects exactly one leader fleet-wide; followers poll the row and adopt the leader's tokens. `update_tokens` gains strict-owner condition (`refresh_lock_owner = :owner`) that atomically `REMOVE`s the lock attrs on successful persist and rejects stale-leader stomps via `ConditionalCheckFailedException`. Absolute-lifetime guard added ahead of lock acquisition so we don't burn a Cognito refresh on a row that's about to TTL-evict (#273, #275)
- Per-message cost double-count on tool-use turns — Strands' `AgentResultEvent` cumulative `accumulated_usage` overwrote the last assistant message's per-call usage via `.update()`. Route the result-extracted cumulative on the `metadata_summary` turn-summary track instead of `metadata` (#270)
- Context-% inflation within a tool turn — Bedrock reports each per-LLM-call `inputTokens` as the full context sent on that call, so Strands' summed `accumulated_usage` over-reports. `stream_coordinator` no longer accumulates `metadata_summary` into `accumulated_metadata`; per-call `metadata` last-write-wins so the value equals the most recent call's full input = current context. Summed across `inputTokens` + `cacheReadInputTokens` + `cacheWriteInputTokens` since `AgentResult.context_size` under-reports by 99%+ under prompt caching (#270)
- `LatencyMetrics.time_to_first_token` changed from `int` (placeholder 0) to `Optional[int]` (placeholder `null`) — a real TTFT can't be 0ms and aggregations need to distinguish absence from a real value (#270)
- Session-expired mid-session left users stranded with a generic toast or no feedback on SSE. Every 401 now flows through `SessionService.handleUnauthorized()`, which dedupes concurrent calls and navigates once with preserved `returnUrl` (#277)
- Session loss not surfaced until the next HTTP call failed. Added cookie-presence fast-path (JS-readable `__Host-bff_csrf` cookie absence implies `__Host-bff_session` also gone) and visibility re-probe on tab refocus (#277)
- Login & first-boot lava-lamp backdrop dark-mode CSS never applied on cold load — `html.dark .X` selectors don't match under Angular's emulated view encapsulation, and `ThemeService` was never injected in the pre-auth tree. Switched to `:host-context(html.dark) .X` and forced `ThemeService` construction via `provideAppInitializer` (#271)
- XLSX→CSV filename mismatches in the Code Interpreter sandbox triggered retry loops. Targeted error hints, tolerant filename matching for CSV↔XLSX aliasing, schema footer preservation on errors

### 🚀 Added

- Server-rendered PDF page-1 thumbnails on attachment cards. New `ThumbnailRenderer` MIME-dispatcher (PDF today via `pypdfium2`, lazy-cached `_thumb.png` sibling in S3, render runs in `loop.run_in_executor`); new `GET /files/{upload_id}/thumbnail` returning a short-lived presigned URL; single-file + session-cascade deletes clean up thumbnails. Frontend: `FileUploadService.getThumbnail()` returns a typed `ready` / `unsupported` / `unavailable` result; PDF badge renders `object-cover` (#263)
- Rich previews in user messages — iMessage-style image mosaic (1-bubble / 2-col / 1+2 split / 2×2 / 5+ with `+N` overlay) with full-screen lightbox + arrow-key navigation; document-style cards for non-images with tinted header + folded corner + content excerpt. New `GET /files/{upload_id}/preview-url` and `GET /files/{upload_id}/text-snippet` (first 2KB UTF-8) (#254)
- Inline markdown preview for `.md` files in attachment cards; full-screen modal viewer via `ngx-markdown` instead of opening raw source in a new tab (#262)
- Spreadsheet analysis tools — `list_spreadsheets` enumerates CSV/XLSX across KB + attachments (with size + MIME metadata); `analyze_spreadsheet` runs Python analysis in Code Interpreter with schema detection (skiprows probing), cleaned pandas/numpy tracebacks, and 10K/600-char output/error truncation. Injected per-request via `extra_tools` (#f88ce7ec, #0ab90bb1)
- `SKIP_AUTH=true` local-dev bypass in `apis.shared.auth.dependencies` returns a fake admin user from all three auth dependencies. Optional tuning: `SKIP_AUTH_ROLES`, `SKIP_AUTH_USER_ID`, `SKIP_AUTH_EMAIL`. Startup guard in `app_api/main.lifespan` refuses to boot when `SKIP_AUTH=true` is paired with any non-localhost entry in `CORS_ORIGINS`. Inference-api intentionally not bypassed (all SPA traffic flows through app-api) (#272)
- New CI workflow `.github/workflows/skip-auth-guard.yml` greps CDK source, workflow files, and Dockerfiles for `SKIP_AUTH=true` / `SKIP_AUTH: true` patterns and fails the build if any leak into deployed config. SHA-pinned `actions/checkout`, `ubuntu-24.04` (#272)
- `SessionRepository.try_acquire_refresh_lock(session_id, owner, lock_ttl_seconds)` and `release_refresh_lock(session_id, owner)` for cross-task refresh coalescing (#273, #275)
- `apis/shared/sessions_bff/single_flight.py` — new `resolve_once(session_id, loader_coro_factory)` primitive for in-process coalescing of the session-resolve path (#264)
- CAUTION comment in `stream_coordinator` documenting that `AgentResult.context_size` / `EventLoopMetrics.latest_context_size` return only `inputTokens`, under-reporting by 99%+ under prompt caching (#270)

### ✨ Improved

- File metadata utilities (`backend/src/apis/shared/files/models.py`) for consistent attachment handling — `FileMetadata`, `FileContent`, size formatting, MIME-type inference — shared between routes and the chat-input component
- Spreadsheet-analysis system prompt clarifies filename vs. sandbox-path handling; tool docstrings expanded with critical guidance on retries
- Stream processor error handling for Code Interpreter responses is more defensive
- Updated `test_session_refresh_preservation.py`'s `InstrumentedTable` to differentiate lock-acquire / token-persist / slide writes so `update_item_side_effect` injection only fires on the persist path (preserving original test intent) (#273)

### 🔒 Security

- `kms:GenerateDataKey` and `kms:DescribeKey` dropped from the app-api runtime task role (least privilege). Only `kms:Decrypt` remains, invoked by Secrets Manager on the caller's behalf when reading the CMK-encrypted `BFFCookieDataKeySecret` (#274)
- `SKIP_AUTH=true` gated by boot-time CORS-origin allowlist + CI guard workflow; fails closed for any deploy target we haven't anticipated instead of blocklisting known cloud env vars (#272)

### ⚡ Performance

- `SessionRefreshMiddleware` resolve path now coalesces Angular's ~8-endpoint page-load fan-out to 1 `get_item` and 0 `update_item` on the critical path (previously ~16 serialized blocking AWS calls per user per minute). Response latency independent of `touch_last_seen` DDB latency after the `_maybe_slide` fire-and-forget refactor (#264)
- `CookieCodec` initialization dropped from `kms:GenerateDataKey` + per-cold-start round trip to a one-shot Secrets Manager `GetSecretValue` + local SHA-256. No more per-task cold-start KMS call (#274)
- Thumbnail render runs in `loop.run_in_executor` so the request worker isn't blocked; lazy `_thumb.png` sibling in S3 means steady-state thumbnails are a HEAD + presign, not a render (#263)

### 🏗️ Infrastructure

- New `BFFCookieDataKeySecret` (Secrets Manager, encrypted with `BFFCookieSigningKey` CMK); SSM parameter `/${projectPrefix}/auth/bff-cookie-data-key-secret-arn` publishes the ARN
- App-api task role: added `secretsmanager:GetSecretValue` on the new secret; removed `kms:GenerateDataKey` and `kms:DescribeKey` on `BFFCookieSigningKey`; kept `kms:Decrypt`
- `appApi.desiredCount` raised 1 → 2 — concurrency slack so a single blocked event loop can no longer halt all ingress

### 📦 Dependencies

- Backend: `strands-agents` 1.37.0 → 1.39.0, `strands-agents-tools` 0.5.1 → 0.5.2, new: `pypdfium2` (#265, #263)

### 🧪 Test Coverage

- `tests/apis/shared/middleware/test_session_refresh_bug_condition.py` (12 cases) — encodes the seven sub-conditions of the event-loop-blocking bug as Hypothesis properties. Fails on unfixed code (by design); passes on fixed code (#264)
- `tests/apis/shared/middleware/test_session_refresh_preservation.py` (19 cases) — locks in 11 preservation invariants that must remain unchanged for non-buggy inputs (#264)
- `tests/apis/shared/sessions_bff/test_single_flight.py` (6 cases) — primitive-level coverage for the new `resolve_once` module (#264)
- `tests/apis/shared/sessions_bff/test_session_refresh_cross_task.py` (480 lines) — two-task integration coverage over moto DDB for the cross-task refresh lock, follower-polling/adoption, TTL recovery, headline invariant that two tasks racing in parallel call Cognito at most once (#273)
- 8 new repository tests for the lock primitive (acquire on unlocked row, contention blocks peer, TTL recovery, distinct-session isolation, release-by-owner-only, atomic clear on token persist, condition fails when peer owns the lock, phantom-row-prevention on acquire, strict-owner release condition, absolute-lifetime guard ahead of refresh) (#273, #275)
- `tests/agents/main_agent/streaming/test_per_message_cost_attribution.py` — three regression cases for the `metadata` vs `metadata_summary` contract; two parametrized cases for `stream_coordinator` current-context semantics including all-three-buckets-summed under cache-read/write (#270)
- `tests/costs/test_calculator.py` — 26 cases of direct coverage for `CostCalculator` (per-bucket pricing, cache scenarios against Sonnet 4.5 rates, defensive missing-key / None handling, `calculate_cache_savings`, `validate_*` predicates) (#270)
- `tests/auth/test_skip_auth.py` — `SKIP_AUTH` dependency-bypass + env-override coverage, startup guard allowlist behavior, skip-auth-guard.yml regex matches (#272)
- Session-wide autouse fixture in `tests/conftest.py` scrubs `SKIP_AUTH_*` env so developer `.env` bleed doesn't silently turn on the bypass in test runs (#272)
- Infrastructure-stack tests: dropped bootstrap-custom-resource assertions; added negative lock that no `AwsCustomResource` emits `kms:GenerateDataKey` / `secretsmanager:PutSecretValue`; positive assertion on `generateSecretString` shape (44-char, no punctuation, no space); fixed two pre-existing stale resource-count assertions (16→18 DDB tables, 3→6 secrets) (#273, #274)

## [1.0.0-beta.24] - 2026-05-06

### 🚀 Added

- BFF Token Handler: cookie-based auth replacing `localStorage` Bearer tokens. Opaque session id in a `__Host-bff_session` httpOnly cookie sealed with AES-GCM under a KMS-wrapped data key; Cognito tokens stored server-side in `BFFSessionsTable`; confidential `CognitoBFFAppClient` (secret in Secrets Manager) for server-side code exchange; `SessionRefreshMiddleware` silently refreshes Cognito tokens; `CSRFMiddleware` enforces double-submit tokens on unsafe methods
- BFF auth routes on app-api: `GET /auth/login` (Cognito PKCE, optional `identity_provider` + `return_to`), `GET /auth/callback`, `GET /auth/session`, `POST /auth/logout` (returns `{post_logout_url}` so the SPA bounces through Cognito Hosted UI to clear the upstream session)
- Cookie-authenticated `POST /chat/stream` SSE proxy to inference-api `/invocations`; owns the `httpx.AsyncClient` lifecycle so headers flush immediately; forwards `OAuth2CallbackUrl` for tool-side OAuth consent scoping; `_build_upstream_url()` percent-encodes the AgentCore Runtime ARN as a single path segment and appends `?qualifier=DEFAULT`
- CloudFront `/api/*` behavior with a viewer-request prefix-strip function; SPA fallback scoped to S3 via a separate viewer-request function so API errors pass through unchanged
- Sliding session lifetime: cookie `Max-Age` and DDB row TTL bump on every successful resolution, capped at `BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS` (default 30 d) and throttled by `BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS`
- Voice mode WebSocket-ticket proxy on app-api: `POST /voice/ticket` + WebSocket `/voice/stream` with HMAC ticket codec, DynamoDB replay store, and per-text-frame `auth_token` / `user_id` injection on the upstream relay (#211, #233)
- Per-conversation cost + context-window badge above the composer, backed by write-time aggregation on the session row; color-graded SVG ring with tooltip showing underlying token counts including cache reads and writes (#223)
- Context compaction SSE event surfaced inline as an "Earlier messages summarized" indicator with cumulative turn count; rehydrates after refresh via `totalSummarizedTurns` on the session-metadata GET (#243)
- Per-model inference parameters with canonical-name translation to provider-native shapes; Anthropic extended thinking via `supportedParams.thinking` with budget validation and temperature/top_p/top_k suppression (#203)
- Settings → Advanced panel for per-request inference-param overrides, persisted in sessionStorage
- Frosted-glass login card with primary-color blob backdrop; respects `prefers-reduced-motion` (#246)
- `GET /admin/auth-providers/cognito-redirect-uri` for admin-only Cognito domain lookup (replaces the retired `/config.json` fetch)
- XLSX-specific RAG chunker with header-row heuristics that skip title/banner rows; multi-sheet name prefix preserves context across embeddings
- Batched S3 Vectors writes (50 vectors per batch) to prevent request-body-size failures on large embedding batches
- AST-based architectural boundary tests enforcing `inference_api`, `agents/`, `apis.shared`, and `app_api` import rules (#200)
- New infrastructure: `BFFSessionsTable`, `BFFCookieSigningKey` (KMS), `CognitoBFFAppClient` + secret, `VoiceTicketReplayTable`, `VoiceTicketSigningSecret`
- `CognitoConfig.supportedIdentityProviders` (env `CDK_COGNITO_SUPPORTED_IDPS`) so the BFF client can federate beyond COGNITO
- `.env.example` now documents Cognito and BFF Token Handler env vars (previously zero coverage)

### ✨ Improved

- BFF refresh-token rotation hardened: rotation writes retry up to three times with 50/100 ms backoff and fail closed if every attempt fails; no-rotation responses take a single best-effort write
- `CookieCodec` promoted to a process-wide singleton so the `/auth/callback` seal and `SessionRefreshMiddleware` unseal use the same KMS-derived key
- SSE proxies (`/chat/stream` and `/chat/api-converse`) now own the upstream `httpx.AsyncClient` lifecycle and close it in the generator's `finally` block so headers flush immediately (#217)
- BFF callback seeds the Users row directly from ID-token claims (`email`, `name`, `picture`, `custom:roles` / `cognito:groups`); fixes first-login users missing email and falling back to Cognito provider-group roles
- Anonymous-user 401 lands on SPA `/auth/login` (with `returnUrl`) instead of Cognito Hosted UI; 401 toasts suppressed while the redirect is in flight (#228)
- `LoginPage` now redirects authenticated users to `returnUrl` instead of requiring a manual Sign In click (#226)
- Migrated `APP_INITIALIZER` to Angular 19+ `provideAppInitializer`; bootstrap's 401 path now hangs the promise so the SPA can't render during the queued redirect (#226)
- Angular build defaults to production via `defaultConfiguration`; `ng serve` defaults to development
- `scripts/gen-version.js` prebuild hook reads the monorepo root `VERSION` file and emits `src/version.ts` so the bundle carries the committed version
- Cost-badge pricing sums per-message metadata (matching the persisted C# records) and includes `cacheReadInputTokens` + `cacheWriteInputTokens` in context-window occupancy
- Compaction state lazy-loads on the AgentCoreMemory existing-session path; prevents default-zero writes overwriting persisted counters on refresh
- `ChatStateService` seeds cost / context signals from session metadata on route change; clears stale state before new metadata loads
- Legacy sessions lazy-backfill `totalCost` and `lastContextTokens` on first read — no migration script required
- `ToolAccessService` catalog now sources from DynamoDB via `freshness.get_all_tool_ids`; admin create/update/delete invalidate the snapshot
- Google's `initiate_consent` path always sends `prompt=consent` so Disconnect/Reconnect actually re-issues a refresh token (#245)
- In-process token cache gained a TTL (default 3000 s) so AgentCore Identity's refresh flow gets a chance to run before the upstream 3600 s lifetime (#210)

### ⚠️ Changed

- **Breaking:** SPA-facing routes no longer accept `Authorization: Bearer`. Cookie auth is required. External callers must migrate to the BFF session flow or hit `/chat/agent-stream` (Bearer-only) instead
- **Breaking:** `POST /chat/stream` is now the cookie-authenticated BFF proxy. The legacy in-process agent loop moved to `POST /chat/agent-stream` for API-key and scripted callers
- **Breaking:** SPA `/auth/callback` route removed. The BFF callback at `${appApiUrl}/auth/callback` is the only OAuth landing
- **Breaking:** SSM parameters `/auth/cognito/app-client-id` and `/oauth/callback-url` deleted. Consumers must migrate to `/auth/cognito/bff-app-client-id` and register a per-system callback URL
- Public PKCE Cognito client decommissioned; `InferenceApiStack`'s runtime authorizer and `AppApiStack`'s `COGNITO_APP_CLIENT_ID` repoint to the BFF client
- `/config.json` runtime fetch retired; `appApiUrl`, `version`, and `cognitoDomainUrl` resolved via build-time injection + a dedicated admin endpoint
- `ConfigService` collapses to a thin signal accessor over `environment.appApiUrl`; `inferenceApiUrl`, `cognitoAppClientId`, `cognitoRegion`, and `environment` fields removed from `RuntimeConfig`
- `apis.app_api.costs`, `apis.app_api.tools.models`, `apis.app_api.storage`, and `apis.app_api.auth.api_keys` moved to `apis.shared.*`. Out-of-tree imports must update (#200)
- `lastTemperature` on `SessionPreferences` and `isReasoningModel` on `ManagedModel` removed; Pydantic v2 `extra="ignore"` handles legacy rows (#203)
- CloudFront origin `readTimeout` capped at the 60 s default max (was 180 s, which failed `InvalidRequest` on distribution update)
- CodeQL and Dependabot workflows retargeted from `develop` to `main` (#247)

### 🐛 Fixed

- CloudFront distribution-wide `errorResponses` rewrote `/api/*` 4xx into 200 + `index.html`; Angular `HttpClient` choked parsing HTML as JSON (#230)
- BFF chat proxy was calling the AgentCore Runtime data plane with the ARN unencoded and no `qualifier`; 404 on every `POST /chat/stream` (#231)
- `CDK_CERTIFICATE_ARN` missing from frontend synth/deploy jobs caused the `/api/*` origin to fall back to `HTTP_ONLY`, breaking same-origin `__Host-` cookie assumptions (#229)
- Frontend CI was building with `development` config on `develop`-branch cloud deploys, bundling `localhost:8000` into the deployed app; Private Network Access blocked loopback calls (#224)
- Trailing commas in `CDK_COGNITO_CALLBACK_URLS` / `CDK_COGNITO_LOGOUT_URLS` produced empty strings Cognito rejected with a regex validation error (#222)
- OAuth-paused agent orphaned after resume because the agent cache keyed on the unbuilt prompt but the snapshot persisted the built one; resume landed on a different slot, the paused agent got cache-hit on the next non-resume turn, Strands raised "must resume from interrupt" (#207)
- Cost summary writer raised `decimal.InvalidOperation` when `MessageMetadata.cost` was a breakdown dict instead of a float; rollup silently went stale (#208)
- `reasoningContent` blocks dropped by session persistence broke subsequent Bedrock calls on thinking + tool use turns (required thinking signature field missing) (#203)
- `ensure_session_metadata_exists` GSI gating (#194) regression test: `preview-chat` spec race where mock pollution in the shared vitest worker pool failed with cryptic "undefined" error instead of a clear assertion
- `preview-chat` test flake from module-level `vi.mock('@microsoft/fetch-event-source')` resolved to a different `vi.fn()` instance under the shared worker pool; replaced with a `FETCH_EVENT_SOURCE` `InjectionToken`
- `cost.service.spec` absorbed stray `resource()` loader request by switching to `httpMock.match(...)` (#225)
- Agentcore-identity tests were failing when local `.env` defined `AGENTCORE_RUNTIME_WORKLOAD_NAME`; autouse fixture now scrubs it (#214)
- Session cost/context signals previously preserved stale values across session changes; seed + reset on route change fixes it (#223)
- Compaction state wrote default zeros on first sub-threshold turn of an existing AgentCoreMemory session; lazy-load on `update_after_turn` fixes the silent undercounting (#243)
- `_merge_inference_params` ungated request-side passthrough could let users submit future canonical keys the admin hadn't bounded; now gated against `KNOWN_CANONICAL_PARAMS` (#203)
- Voice WS config-frame injection was a one-shot flag; a SPA sending any non-config text frame first could consume the slot and let subsequent config frames forge identity. Injection now runs on every text-type frame and overwrites `user_id` (#233)
- Cross-origin `HttpClient` requests to app-api now carry the BFF cookie via a new `withCredentialsInterceptor`; previously 160+ calls 401'd after a successful cross-origin login (#221)
- `/auth/callback` same-origin `return_to` splice grafts the scheme + netloc from `BFF_POST_LOGIN_REDIRECT_URL` onto the path so cross-origin dev (`:8000` → `:4200`) lands on the SPA origin (#221)

### 🔒 Security

- BFF `return_to` control-byte bypass closed — `_sanitized_return_to` rejects all C0 control bytes (U+0000..U+001F), not just CR/LF, defeating browser URL-parser strip tricks like `/\t/evil.com` (#221)
- AES-GCM cookie codec now binds the cookie version byte into associated data and stops swallowing KMS infrastructure errors as decode failures (transient KMS hiccups no longer log every active user out) (#213)
- BFF session-cookie tokens validated against `COGNITO_BFF_APP_CLIENT_ID` by a separate validator instance; the SPA validator's client_id check would have rejected every BFF-issued token (#213)
- Pygments 2.19.2 → 2.20.0 (ReDoS in GUID-matching regex, Dependabot alert #71) (#247)
- CodeQL remediation: log-injection on user-controlled `model_id` and other inputs, unused imports/locals across infrastructure, explanatory comments on empty-except blocks (#247)
- Markdown-rendered links remain `rel="noopener noreferrer"` (carried from beta.23)
- Dependabot security alerts resolved: pillow 12.2.0, cryptography 47.0.0, python-multipart 0.0.27, aiohttp 3.13.5, uuid 14.0.0 (#199)

### 📦 Dependencies

- Backend: `pillow` 12.2.0, `cryptography` 47.0.0, `python-multipart` 0.0.27, `aiohttp` 3.13.5, `pygments` 2.19.2 → 2.20.0
- Frontend Angular: `@angular/*` 21.2.7 → 21.2.11, `@angular/cdk` 21.2.5 → 21.2.9, `@angular/build` / `@angular/cli` 21.2.6 → 21.2.9
- Frontend minor/patch group: `tailwindcss` 4.2.2 → 4.2.4, `vitest` 4.1.2 → 4.1.5, `ngx-markdown` 21.1.0 → 21.2.0, `@ng-icons/*` 33.2.0 → 33.2.2, `postcss` 8.5.8 → 8.5.12, `jsdom` 29.0.1 → 29.1.0, `fast-check` 4.6.0 → 4.7.0, `uuid` 13.0.0 → 14.0.0
- Frontend dev: `@analogjs/vite-plugin-angular` 3.0.0-alpha.26 → 3.0.0-alpha.53, `@analogjs/vitest-angular` 3.0.0-alpha.26 → 3.0.0-alpha.30
- Frontend transitive overrides: `vite >= 7.3.2`, `dompurify >= 3.4.0`, `lodash-es >= 4.18.0`, `hono >= 4.12.14`, `@hono/node-server >= 1.19.13`, `undici < 8.0.0` (jsdom compatibility), mermaid's nested `uuid` pinned to 14.0.0
- Infrastructure: `aws-cdk-lib` 2.248.0 → 2.251.0, `aws-cdk` 2.1117.0 → 2.1120.0, `@types/node` 25.5.2 → 25.6.0

### 🏗️ Infrastructure

- New resources: `BFFSessionsTable`, `BFFCookieSigningKey` (KMS), `CognitoBFFAppClient` + secret in Secrets Manager, `VoiceTicketReplayTable`, `VoiceTicketSigningSecret`
- CloudFront `/api/*` behavior on the frontend distribution with viewer-request prefix-strip function; SPA fallback moved from distribution-wide `errorResponses` to a viewer-request function on the S3 behavior
- CloudFront origin `readTimeout` capped at 60 s (CloudFront default max without a service-quota increase)
- Public PKCE Cognito client decommissioned; SSM parameters `/auth/cognito/app-client-id` and `/oauth/callback-url` removed
- `InferenceApiStack` runtime authorizer repointed to `/auth/cognito/bff-app-client-id`
- `AppApiStack` `COGNITO_APP_CLIENT_ID` env repointed to the BFF client; new env vars: `BFF_AUTH_CALLBACK_URL`, `BFF_POST_LOGIN_REDIRECT_URL`, `BFF_SESSION_ABSOLUTE_LIFETIME_SECONDS`, `BFF_SESSION_SLIDING_RENEWAL_THROTTLE_SECONDS`, `VOICE_TICKET_*`, `INFERENCE_API_URL`
- IAM grants on app-api: Secrets Manager read for BFF client secret + voice ticket signing secret, KMS `GenerateDataKey`/`Decrypt` on the cookie signing key, DynamoDB CRUD on sessions and voice ticket replay tables
- `FrontendStack`: `/config.json` `BucketDeployment` and invalidation removed; `runtimeConfig` object gone; `/auth/cognito/domain-url` SSM lookup removed

### 🔧 CI/CD

- CodeQL and Dependabot workflows retarget from `develop` to `main` (#247)
- Frontend cloud builds pinned to `BUILD_CONFIG=production` (#224)
- `CDK_CERTIFICATE_ARN` added to frontend synth/deploy jobs (#229)
- `CDK_AWS_ACCOUNT` surfaced as E2E variable
- Seed script integrated into E2E workflow for bootstrap data provisioning
- RAG-ingestion workflow path filters include `backend/src/apis/shared/embeddings/**`

### 🧪 Test Coverage

- BFF session handler: codec round-trip + tamper rejection, CSRF validation, repository CRUD with TTL, multi-tab refresh-token-storm coalescing (asserts N concurrent requests for the same session drive exactly one Cognito refresh exchange)
- BFF chat SSE proxy: auth gate, header/body/URL relay, SSE and non-SSE paths, upstream 4xx/5xx propagation, `ConnectError` → 502, `TimeoutException` → 504, CSRF missing/mismatch/valid, TTFB < 200 ms integration test backed by a real uvicorn server with a slow upstream
- Voice ticket: 30 backend + 2 frontend tests (codec, replay, service, URL builder, config-frame injection on every text frame, route auth gates)
- New `tests/apis/inference_api/test_chat_service.py` covering the paused-agent cache-eviction fix (#207)
- `tests/architecture/test_import_boundaries.py` AST-based boundary enforcement (#200)
