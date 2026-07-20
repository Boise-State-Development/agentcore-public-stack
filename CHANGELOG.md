# Changelog

All notable changes to this project are documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For narrative release notes written for operators and product owners, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [1.9.0] - 2026-07-20

Feature release making **Bedrock prompt-cache economics stable and measurable**. Three cache-busting defects in the model-call path are fixed (per-turn history mutation, nondeterministic skill ordering, single-cachePoint fragility), and a new observability layer makes every model call's cache behavior diagnosable: prefix fingerprints and a `cacheStatus` classification on each cost row, an admin Session Cost Anatomy drill-down page, CloudWatch EMF metrics, and a dashboard with alarms. Also fixes chat-input textarea sizing and points the deployed runtime's Word-document tools at the real user-files bucket. Requires a CDK deploy (new dashboard construct + one runtime env var); no data migration.

### 🚀 Added

- Prompt-cache observability per model call — `PrefixFingerprintHook` hashes the three cacheable prefix components (toolConfig, effective system prompt, message history) onto each cost row; write-time `cacheStatus` classification (`first_write | hit | miss_ttl_expired | miss_avoidable | uncached`) with `wastedUsd` for avoidable misses priced from the row's own pricing snapshot; session-row cache rollups (`totalCacheReadTokens`, `totalCacheWriteTokens`, `avoidableMissCount`, `wastedUsd`); `GET /admin/costs/sessions/{sessionId}/calls` admin endpoint; and per-call CloudWatch EMF metrics. Kill switch `PROMPT_CACHE_OBSERVABILITY_ENABLED=false` (default ON) (#697)
- Admin **Session Cost Anatomy** page at `/admin/costs/sessions/:id` — chronological per-call table with color-coded `cacheStatus` badges and prefix-fingerprint diffing that names which hash (tools/system/history) flipped versus the previous call, plus session-level cache summary and a session-id lookup form on the Cost Analytics dashboard (#700)

### ⚡ Performance

- Restored conversation history is now byte-stable between turns — tool-content truncation is driven by a persisted `truncation_anchor` in compaction state instead of a sliding window that re-mutated history every turn, which forced a full prompt-cache prefix re-write (~$2.50/MTok on a 35k–150k-token prefix) nearly every turn (#697)
- Skill records reach the `<available_skills>` system-prompt block in deterministic order (sorted at the repository, RBAC-union, and injection layers), removing a per-turn prompt-cache invalidator (#697)
- Model calls now carry 3 of Bedrock's max-4 cachePoints (toolConfig tail, system tail, last-user-message) so a message-level cache miss still reads the stable tools+system prefix from cache instead of re-writing everything; the added points are gated to models that support them (#697)

### 🐛 Fixed

- Chat input textarea is scrollable once content exceeds its max height, clamps its growth, and resets to its base height after sending (#696)
- The first cache write after a run of below-threshold (uncached) calls is classified `first_write`, not `miss_avoidable` — it no longer inflates the AvoidableMiss/WastedUsd metrics or the Session Cost Anatomy page (#701)
- Word-document tools on the deployed runtime save to the real user-files bucket — `S3_USER_FILES_BUCKET_NAME` is now set on the inference-api Runtime, where its absence made the tools fall back to a literal `user-files` bucket and fail with `AccessDenied` (#702)
- Word-document tools fail fast with a clear "storage is not configured" message when the bucket env var is unset, instead of surfacing a confusing S3 `PermanentRedirect`/`AccessDenied` mid-run (#706)

### 🏗️ Infrastructure

- `PromptCacheObservabilityConstruct` (`lib/constructs/observability/`) — CloudWatch dashboard over the `AgentCoreStack/PromptCache` EMF metrics (cache read/write tokens, cache-efficiency expression, AvoidableMiss, WastedUsd) with a Logs Insights widget grouped by `cacheStatus`, plus console-only alarms on AvoidableMiss and WastedUsd (stricter in prod; `NOT_BREACHING` on missing data so the kill switch stays quiet) (#699)
- `S3_USER_FILES_BUCKET_NAME` added to the inference-api AgentCore Runtime environment (#702)

### 📚 Docs

- CLAUDE.md — prompt-cache determinism/byte-stability contract, the fingerprint-based cache-miss debugging recipe, and a token-cost-effectiveness design tenet (#697, #698)

## [1.8.0] - 2026-07-19

Feature release delivering **Skills v2** — skills are redesigned from tool-binding containers into pure, portable knowledge bundles that Agents load on demand, and for the first time **any signed-in user can author their own**. Skills ship enabled by default. Also completes the session-metadata static-sort-key migration (issue #175 Phases 2–3) with an operator backfill and a GSI-only read contraction, and consolidates the two artifact tool-catalog rows into a single "Artifacts" toggle. **Three backfill scripts must be run per environment** — see [RELEASE_NOTES.md](RELEASE_NOTES.md) deployment notes. No new AWS resources; a CDK deploy is needed only to set the `SKILLS_ENABLED` env var explicitly.

### 🚀 Added

- User-authored skills tier ("My Skills") — owner-scoped CRUD at `/skills/mine/*` over a new user tier in the app-roles table (GSI4-indexed), with bundle file upload, plus a `/my-skills` page and form in the SPA. Every route resolves ownership through `UserSkillService`; a skill you do not own is indistinguishable from one that does not exist (#683)
- Skills selection surfaces — a "Skills" section in the chat model-settings panel listing every skill a user can reach (RBAC-granted catalog skills **union** their own), each with an opt-in toggle, plus **invoke-through**: an Agent shared to an ordinary user resolves its bound skills for them even if they could not select those skills themselves (#684)
- `read_skill_file` progressive-disclosure tool and agentskills.io bundle support — skills are stored as portable S3 bundles with a `SKILL.md` write-through projection, so a skill prefix can be handed to a managed Harness or exported as-is (#681)

### ✨ Improved

- Session listing contracts to a single GSI query once the migration marker is set (issue #175 Phase 3) — the legacy base-table query is skipped after the Phase 2 backfill writes `PK=MIGRATION#session-sk`. Fails open: an absent marker or a GSI error keeps the dual-read path, so the deploy is order-independent and forks that never run the backfill are unaffected. The marker result is memoised only on success, so a container started before the backfill picks up the flip without a restart (#678)
- Static-SK backfill for the cold tail (issue #175 Phase 2) — `backend/scripts/backfill_session_static_sk.py` migrates legacy session rows and removes ghost stubs. Dry-run by default, idempotent, and `--set-marker` refuses to write the completion marker while any legacy rows remain (#677)

### ⚠️ Changed

- **Breaking (conceptual):** skills no longer bind tools. A skill is now instructions + reference files only; `allowedTools` persists as advisory metadata and **never grants a tool**. The skills mode toggle is gone, and the runtime moved to the Strands `AgentSkills` plugin with `agent_type="skill"` kept as a temporary ChatAgent alias. Existing skill rows keep working; their tool lists simply stop granting (#680, #681)
- Artifact tool catalog collapsed from two rows to one — "Create Artifact" and "Update Artifact" are now a single **"Artifacts"** toggle (`toolId` `create_artifact`), which injects both tools at runtime. **Requires `backend/scripts/backfill_artifact_tool_merge.py`** to promote existing role grants, user preferences, and assistant bindings before the retired row is deleted (#689)
- The `skills` RBAC capability gate was removed from the user-facing skills routes. It could not be granted from the admin roles UI — that form builds `grantedTools` from the tool catalog, and a capability id is not a tool — so an admin granting a catalog skill to a role would have found it silently invisible with no in-product fix. Access is now `SKILLS_ENABLED` per environment plus a role's `grantedSkills` per cohort (#692)
- The "My Skills" sidenav entry is hidden pending a navigation decision; the `/my-skills` route, page, and backend surface remain live and reachable by direct URL (#690)

### 🐛 Fixed

- Saving an Agent with an invalid form no longer looks like a click that did nothing — the first invalid control is scrolled into view and focused, with an explanatory toast. Also corrects stale copy on the agent and admin skill forms that still claimed skills carry their own bound tools (#682)

### 🏗️ Infrastructure

- `SKILLS_ENABLED` is threaded into both app-api and inference-api from `config.skills.enabled` (default ON with a `CDK_SKILLS_ENABLED=false` kill switch). No new AWS resources — the skill-resources S3 bucket and app-roles table already exist. Note that deployed containers set no `SKILLS_ENABLED` today, and unset now reads as enabled, so **skills activate on the `backend.yml` deploy, before any CDK deploy** (#685)

### 📚 Docs

- Skills v2 design spec — skills as a pure knowledge primitive bound on Agents (#679)
- Agent Directory spec (#688) and Skill Creator spec (#691) — design documents for unbuilt features

## [1.7.1] - 2026-07-17

Patch release fixing a Word-document save failure in the AgentCore Runtime and advancing the **session-metadata static-sort-key migration** (issue #175) to its write side. Word tools now resolve the user-files bucket's real region instead of pinning to `AWS_REGION`, so `PutObject` no longer fails with `PermanentRedirect`. On the storage side, new sessions are now born with a static sort key and legacy rows self-migrate in place on their next write, structurally eliminating the ghost-row race behind the "Failed to parse session item" warnings and closing the first-turn duplicate-row race. No infra or data migration; ship through `backend.yml`.

### ✨ Improved

- Static-sort-key write path for session metadata (issue #175 Phase 1b) — new sessions are born at a static sort key (`S#{session_id}` + `SessionRecencyIndex` keys) guarded by a real `attribute_not_exists(PK)` conditional put, and legacy rows do a one-time in-place migration to the static SK on their next write. Rows no longer rotate on every message, so the ghost-row race that produced "Failed to parse session item" warnings is structurally eliminated for migrated rows, and the deterministic SK closes the first-turn duplicate-row race the old timestamped SK could not gate. `delete_session` resolves the raw SK via the GSI (catching migrated rows the old reconstruction missed) and soft-deletes in place. The recency index is updated (`SET GSI4`) for active rows and removed for deleted ones (#673)

### 🐛 Fixed

- Word-document `PutObject` no longer fails with `PermanentRedirect` in the AgentCore Runtime — the user-files S3 client pinned its endpoint to `https://s3.{AWS_REGION}.amazonaws.com`, but the runtime's `AWS_REGION` does not reliably match the bucket region and the explicit `endpoint_url` disabled botocore's automatic region redirect. The client now resolves the bucket's true region via `HeadBucket` (`x-amz-bucket-region`, backed by the `s3:ListBucket` the runtime role already has) and drops the hardcoded `endpoint_url`, fixing both the save and the presigned download URL (#674)

## [1.7.0] - 2026-07-17

Feature release adding a full **Word (.docx) document toolset** for the agent and advancing the **session-metadata static-sort-key migration** (issue #175) through its read-side phases. The agent can now create, modify, list, and read Word documents — rendered inline in chat with a download button — behind the `create_word_document` capability toggle. On the storage side, a new sparse `SessionRecencyIndex` GSI plus a dual-scheme union reader let session listing work whether or not a session's base sort key has been migrated, deploying safely in any order. Also bumps `strands-agents` to 1.48.0 to fix an "Agent force-stopped" crash on non-PDF document uploads. Requires a CDK deploy for the new GSI; ships the rest via `backend.yml` + the frontend pipeline.

### 🚀 Added

- Word document tools — `create_word_document`, `modify_word_document`, `list_word_documents`, and `read_word_document`, each running `python-docx` inside a Bedrock Code Interpreter session and persisting to the existing user-files store (S3 + DynamoDB). Injected per-request via `_build_word_document_tools`, gated by the single `create_word_document` capability toggle, and seeded into bootstrap `DEFAULT_TOOLS` as "Word Documents". A new frontend `word_document` inline-visual renderer shows the generated file with an accessible download button (#670)

### ✨ Improved

- Dual-scheme union read for session listing (issue #175 Phase 1a) — `list_user_sessions` now reads the union of legacy (base-table `S#ACTIVE#` sort key) and migrated (`SessionRecencyIndex` GSI) sessions, so a session is visible regardless of migration state. Pagination uses a self-derived value cursor (`{lastMessageAt}#{session_id}`) with no cross-page buffering, and undecodable/legacy cursors fall back to the first page across the deploy boundary. No writes change and no row migrates in this phase (#667)

### 🐛 Fixed

- Session listing degrades to legacy-only when `SessionRecencyIndex` is absent — the Phase 1a reader caught only `ResourceNotFoundException` (what moto raises), but real DynamoDB raises `ValidationException` ("The table does not have the specified index") for a missing GSI, so a 1a backend deployed ahead of the CDK GSI would 503 instead of degrading. The catch now also handles the scoped `ValidationException`, restoring order-independent deploys (#669)
- "Agent force-stopped" on non-PDF document uploads — auto prompt caching appended its `cachePoint` after the last user message's content, so any turn attaching a `.txt`/`.docx`/`.csv`/… document sent `[text, document, cachePoint]` and Bedrock's Anthropic adapter rejected it with `messages.N.content.M.type: Field required`. Bumping `strands-agents` to 1.48.0 places the cache point before the first non-PDF document block instead (upstream issue #1966); every placement verified live against ConverseStream (#668)
- Restore-time content-block sanitizer — `TurnBasedSessionManager` now drops empty/typeless content blocks from restored history that could trigger Bedrock ConverseStream `messages.N.content.M.type: Field required` on resume (#670)

### 🏗️ Infrastructure

- New sparse `SessionRecencyIndex` GSI on the sessions-metadata table (`GSI4_PK=USER#{id}`, `GSI4_SK={lastMessageAt}#{session_id}`, projection ALL) for newest-first active-session listing once the base sort key becomes static (issue #175 Phase 0). Adding the index is a no-op until rows populate its keys, so it deploys safely ahead of any code change; IAM is already covered by the `SessionsMetadataAccess` `index/*` wildcard (#666)

### 📦 Dependencies

- `strands-agents` 1.47.0 → 1.48.0 (cachePoint-before-document fix, upstream #1966) (#668)

## [1.6.1] - 2026-07-16

Patch release fixing two agent-invocation regressions. Agents bound to a Mantle-provider model (e.g. `openai.gpt-5.4`) no longer misroute to Bedrock and fail with "invalid model identifier" — the invocation path now backfills the model's registered `provider` server-side. And interrupt-resume turns (OAuth-consent or tool-approval flows, most visibly "connect to Gmail") no longer 500/424: `effective_enabled_tools` is now bound on the resume branch. No infra or migration; ship through `backend.yml`.

### 🐛 Fixed

- Agent-bound invocations resolve the model provider correctly: agent (assistant) bindings persist only `model_id`, so an agent bound to a Mantle model resolved to `provider=None` and misrouted to Bedrock ConverseStream, which rejected it with "The provided model identifier is invalid" even though the same model works from normal chat. `_resolve_model_settings` now also returns the model's registered `provider` and the invocation path (plus the app-tool-call / app-context-update rebuild paths) backfills `effective_provider` from it — fixing all existing provider-less bindings with no data backfill. The Agent Designer save payload now also persists the selected model's `provider` so new bindings are self-describing (#661)
- Interrupt-resume turns no longer crash with `NameError: effective_enabled_tools`: on resume (OAuth-gated MCP consent or tool-approval) the `stream_with_quota_warning` closure referenced `effective_enabled_tools` unconditionally, but it was only assigned on the non-resume branch — so the closure raised before its first yield, the inference-api container returned 500, and the Runtime translated it to a 424 for app-api and the SPA. This broke every interrupt-resume turn since the agent-designer tool-binding refactor (most visibly the "connect to Gmail" OAuth-consent resume). The variable is now bound from the paused-turn snapshot on the resume branch (#662)

## [1.6.0] - 2026-07-15

Conversation-sharing and chat-reliability release. Large conversations can now be shared — their snapshots offload to a new S3 bucket instead of overflowing the 400 KB DynamoDB item limit. **Stop** now actually stops the server-side turn (distributed cancellation over the session lease), and a per-session single-flight lease plus restore-time history repair close a class of bugs where a tab switch or duplicate invocation could permanently brick a conversation. Web sources become removable and editor-manageable, and model RBAC grants written from the model admin page finally take effect. Requires a CDK deploy for the new shared-conversations S3 bucket and IAM grants.

### 🚀 Added

- Share large conversations: snapshot bodies (messages + metadata) offload to a new private `shared-conversations` S3 bucket with a `body_ref` pointer in DynamoDB, so conversations over the 400 KB item limit can be shared. Legacy inline shares still read back with no migration; storage-unavailable surfaces as a friendly 503 (#658)
- Distributed turn cancellation — **Stop** now ends the running server-side turn instead of only the client stream. The app-api `user_stopped` endpoint stamps an owner-scoped `cancelRequestedFor` on the session lease; the inference-api heartbeat (tightened 30s→10s) observes it and cooperatively tears down both the tool loop and the model stream, persisting the partial and releasing the lease. Shrinks the 409-on-resend window from a full turn to ~one heartbeat and halts wasted model/tool spend after Stop (#656)
- Remove a web source: `DELETE /assistants/{id}/web-sources/crawls/{crawl_id}` removes the crawl's sync policy, soft-deletes every page under its root URL (vector + S3 teardown via the existing background cleanup), then hard-deletes the crawl row. In-flight crawls are refused with a 409; a zombie 'running' crawl whose process died stays deletable. Edit-gated (owner or editor) (#648)

### ✨ Improved

- Editors (not just owners) can now start and view web crawls — `start_crawl`, `list_crawls`, and `get_crawl` route through the shared `_require_edit_permission` gate, so the "Add web content" button the SPA already renders for editors no longer 404s; viewers get a clean 403 (#650)

### 🐛 Fixed

- Model RBAC is now single-source-of-truth: the model admin page's role picker writes through to each role's `grantedModels` (mirroring tools/skills) instead of onto a dead `allowedAppRoles` field no access check read, so enabling a model for a role actually grants it. `allowedAppRoles` is derived on read; `can_access_model` and `filter_accessible_models` share one `_grants_access` predicate so a model can no longer be listed by the catalog yet denied on use. Removes the dead `POST /sync-roles` endpoint (#651)
- SSE chat stream stays open across tab switches — `@microsoft/fetch-event-source` `openWhenHidden` is now `true`, stopping the library from aborting and reopening the connection (a fresh `POST /invocations` for the same turn) on `visibilitychange`, which spawned a concurrent backend agent and corrupted tool-pairing history (#653)
- Restore-time tool-use/tool-result pairing repair — `TurnBasedSessionManager._repair_tool_pairing` unconditionally rebuilds a Bedrock-valid history on restore (one result turn per toolUse turn, duplicate/orphaned results dropped, same-role turns merged), recovering conversations already bricked by a "toolResult blocks exceed toolUse blocks" ValidationException. No-op on healthy history (#653)
- Reject duplicate concurrent turns — a per-session single-flight lease at the inference-api `/invocations` chokepoint (atomic conditional write on a `LEASE#{sid}` item) rejects a duplicate turn with 409 so two agent loops can't run against one Memory session; the SPA shows a soft "Already responding" notice instead of a hard error. Resume / max-tokens continuation force-acquire; fail-open on non-conflict DynamoDB errors (#655)
- Guard synthetic error persistence against role-alternation breaks — `persist_synthetic_messages` now drops a synthetic "⚠️ Something went wrong" turn that would land adjacent to a same-role turn, preventing the consecutive-assistant-message amplifier that turned one errored turn into a permanently bricked session (#654)
- Conversation-share operations no longer fail with a generic 500 — the app-api task role is granted DynamoDB access (including the `SessionShareIndex` GSI) on the shared-conversations table it was already wired to via env var, fixing `PutItem`/`Query` AccessDeniedException on share create/list (#657)

### 🏗️ Infrastructure

- New private `shared-conversations` S3 bucket (SSE-S3, versioned) with an SSM param, a `PlatformComputeRefs` entry, the `SHARED_CONVERSATIONS_BUCKET_NAME` app-api env var, and an app-api-only `SharedConversationsBucketReadWrite` IAM grant (#658)
- `SharedConversationsAccess` added to the app-api `coreTables` grant list so the role gets the standard DynamoDB action set on the shared-conversations table and its GSIs (#657)

### 🔧 CI/CD

- Repointed `test_cache_savings.py` storage patch targets to `apis.shared.storage` (the accessor moved; `app_api.storage` is now an empty stub) and gated the compaction integration tests on an explicit `RUN_AGENTCORE_INTEGRATION_TESTS=1` opt-in so leaked env vars no longer make them run order-dependently against invalid credentials (#652)

## [1.5.0] - 2026-07-13

Model-catalog and MCP-admin expansion, plus UI polish. Admins can now discover the tools behind OAuth-gated MCP servers (e.g. the GitHub remote MCP server) using their own vaulted 3LO token, two new curated model cards land (Claude Sonnet 5, GPT-5.4), and the max-output-tokens field goes optional so reasoning/Responses-API models without a fixed cap can be added. Alongside: sticky admin/settings sidebars, a redesigned 404 page, chat-scroll and sticky-nav fixes, a vitest flake fix, and a Docker curl security patch. No CDK deploy, no data migration, no breaking changes — ships via the backend and frontend pipelines.

### 🚀 Added

- Admin tool "Discover" now works against OAuth-gated MCP servers: pass the OAuth provider id and discovery connects with the admin's own vaulted 3LO token (fetched via AgentCore Identity `get_token_for_user`, injected as a bearer), validating the admin's connection and listing the tools their token can see. Previously OAuth-gated servers were refused outright (400) or connected unauthenticated and 401'd. `requires_consent` → 409; unknown provider / forward-auth conflict / oauth2-without-provider → 400 (#639)
- Two curated model catalog cards: **Claude Sonnet 5** (Bedrock, `global.anthropic.claude-sonnet-5`, 1M context, effort-based reasoning, caching on) and **GPT-5.4** (Mantle, `openai.gpt-5.4`, Responses API). Bedrock Claude cards now order most-capable-first and the "Bedrock Mantle" tab sits next to "Bedrock" (#641)

### ✨ Improved

- Max output tokens is now optional on the admin model form — reasoning / Responses-API models (GPT-5.x, adaptive-thinking Claude) that share output with reasoning budget have no fixed cap to enter. `max_output_tokens` is `Optional[int]` end to end; it was only ever a ceiling for the admin-configured `max_tokens` param and is never sent to the provider, so leaving it unset is safe (#643, #644)
- Admin and user-settings sidebar navs are now sticky on desktop, staying in view as the content column scrolls (#632, #638)
- The 404 page was redesigned to match the auth/first-boot screens (frosted glass, animated blobs, graph-paper grid) (#633)
- Chat scroll space is now sized to the pending response and adapts to the shell scroll container, so the last turn spaces correctly (#637)

### 🐛 Fixed

- Mantle `google.gemma-4-*` models 401'd with `access_denied` because Gemma 4 is served only on Mantle's `/openai/v1` path but the Strands SDK routed it to `/v1`. The SDK's OpenAI-path prefix table is now extended with `google.gemma-4-` at build time (lazy, idempotent, guarded); Gemma 3 stays on `/v1` (#641)
- The admin/settings sticky sidebars needed a real shell scroll container to anchor against — the app shell now scrolls on the correct element so `position: sticky` engages (#634)
- Vitest runs now guarantee the Angular JIT compiler is present, stopping the intermittent `PlatformLocation` provider flake in the SPA unit suite (#636)

### 🔒 Security

- Dockerfiles pin curl to `+deb13u*` instead of an exact point release, so the security patch floats with Debian's mirror (which purges superseded point versions on each CVE update) and builds don't break; the digest-pinned base image still provides reproducibility (#645)

### 🔧 CI/CD

- `scripts/common/sync-version.sh` is now portable across GNU and BSD userlands (macOS `sed`/`grep`), so the version bump runs outside the dev container (#631)

### 📚 Docs

- Added the quota cooldown-windows + platform-ceiling spec and committee one-pager (`docs/specs/`) (#635); added a design note proposing `mantleEndpointPath` as a live admin setting (#641)

## [1.4.0] - 2026-07-10

Opt-in MCP user-identity forwarding, plus an admin OAuth-provider repair. A new Cognito Pre-Token-Generation Lambda can copy configured user-pool attributes into namespaced claims on the access token — the only token forwarded end-to-end to MCP servers — so personalized MCP tools can identify the caller. It ships disabled by default (a fork that configures nothing gets zero resources). Also fixes a 502 when adding the first admin OAuth provider. A platform (CDK) deploy is required for the OAuth-provider IAM fix.

### 🚀 Added

- MCP user identity forwarding: an opt-in Cognito Pre-Token-Generation v2 Lambda copies configured user-pool attributes into namespaced claims on the access token forwarded to MCP servers, so personalized MCP tools can identify the caller — no changes to the SPA → app-api → inference-api → MCP path, since the access token was already the token forwarded end to end. Fail-open (any handler error returns the event unchanged, so login is never blocked) and stdlib-only. Enabling requires the Cognito Essentials feature plan plus `CDK_MCP_TOKEN_ENRICHMENT_ENABLED` + `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS`; disabled by default (#627)

### 🐛 Fixed

- Admin "add OAuth provider" (`POST /admin/oauth-providers/`) returned a 502 Bad Gateway on the first provider create. AgentCore's `CreateOauth2CredentialProvider` lazily ensures the default token vault exists, which requires `bedrock-agentcore:CreateTokenVault` (+ `GetTokenVault`) on the caller — grants the app-api task role was missing, so the AccessDenied surfaced as a 502 via the shared AWS-ClientError handler. Both actions are added to the `AgentCoreWorkloadIdentityAccess` statement (#628)

### 🏗️ Infrastructure

- New opt-in `token-enrichment` Lambda (real-code `fromAsset`, attached to the user pool via Cognito `addTrigger` `V2_0`) wired conditionally into `PlatformStack`; the pool's `featurePlan` is pinned to `ESSENTIALS`. Inert unless `CDK_MCP_TOKEN_ENRICHMENT_ENABLED=true` — the committed `cdk.context.json` stays inactive (#627)
- app-api task role: `bedrock-agentcore:CreateTokenVault` + `GetTokenVault` added to the `AgentCoreWorkloadIdentityAccess` statement (scope `token-vault/*` already covered `token-vault/default`; only the actions were missing) (#628)

## [1.3.0] - 2026-07-10

Bedrock Mantle expansion and an API-key endpoint repair. Mantle models gain declarative per-model `apiMode` (Chat Completions vs Responses API) and `region` fields on Strands' `bedrock_mantle_config`, unlocking Responses-only models like `openai.gpt-5.x`; the API-key `POST /chat/api-converse` endpoint — broken in cloud since the BFF/runtime migrations — is relocated onto app-api and now serves the full model catalog including Mantle. Plus Memory Spaces and Scheduled Runs follow-ups. No breaking changes; a platform (CDK) deploy is required for two app-api IAM grants.

### 🚀 Added

- Per-model Mantle API mode + region: admin model records gain `apiMode` (`chat` | `responses`) selecting `OpenAIModel` vs `OpenAIResponsesModel`, and an optional `region` override driving both the Mantle endpoint host and SigV4 token region — Responses-only models (e.g. `openai.gpt-5.x`) now work, and a model can pin inference to its host region independent of where the app runs (#620)
- API-key `/chat/api-converse` now serves the full model catalog: `provider="mantle"` models route through a shared `build_mantle_model` builder (`apis/shared/models/mantle.py`, also used by the agent factory), whose Strands `.stream()` yields the same Converse-shaped events as the Bedrock path so SSE translation and cost accounting are shared (#621)
- The agent can read and keep a Memory Space's `MEMORY.md` index in sync via the reserved `MEMORY.md` slug on `memory_read` / `memory_write` (viewer+ read, editor+ write; the slug is reserved and can't become an ordinary entry) (#614)
- The scheduled-run form targets Agents (the Agent Designer primitive) instead of Assistants; the manual tool picker hides when an Agent is selected (its tool bindings replace `enabled_tools` at invocation), and "Run now" honors the selected Agent (#615)

### ⚠️ Changed

- Scheduled Runs are no longer gated by the `scheduled-runs` RBAC capability — only the `SCHEDULED_RUNS_ENABLED` kill switch remains (404 when off). The surface stays low-key (no nav entry, reachable by direct URL); runs still execute with the caller's own RBAC-allowed tools (#617)
- Mantle inference plumbing moved onto Strands' `bedrock_mantle_config` (the SDK owns the base URL, model-family base path, and bearer-token minting); `mantleEndpointPath` is deprecated (accepted-but-ignored, no stored record breaks) and removed from the admin UI. Gemma 4 is temporarily un-curated pending upstream `google.gemma-` family-prefix routing (#620)

### 🐛 Fixed

- API-key `POST /chat/api-converse` was broken in cloud: app-api proxied it to inference-api, whose AgentCore Runtime data plane only serves `/invocations` + `/ping` (`UnknownOperationException`). The handler is now a self-contained app-api route (validate key → RBAC → Bedrock converse → cost accounting) with no inference-api hop (#621)
- Settings API-key code snippets now resolve a relative `appApiUrl` against the current origin, targeting `<origin>/api/chat/api-converse` — the bare-origin URL missed CloudFront's `/api/*` backend routing and returned a 403 (#621)
- Regular users no longer hit a 403 "Access Denied" toast on page load — the sidenav's background schedules probe tripped the beta-cohort RBAC gate through the global error interceptor; the vestigial probe is also removed (#617)
- Memory Space entries with namespaced slugs (e.g. `people/brian-bolt`) no longer 404 on view/edit/delete — the entry routes use a `{slug:path}` converter so the embedded slash survives routing (#614)

### 🏗️ Infrastructure

- app-api task role: the Bedrock invoke statement gains `bedrock:InvokeModelWithResponseStream` plus all-region foundation-model and account-level inference-profile ARNs, and the project-scoped Mantle statement gains `bedrock-mantle:CreateInference` — both required by the relocated api-converse handler (#621)

### 📦 Dependencies

- Backend: `strands-agents` 1.40.0 → 1.47.0 (and the `[bidi]` extra), added `aws-bedrock-token-generator` 1.1.0 (#619)

### 📚 Docs

- Release workflow consolidated into a single auto-invoked "cutting a release" steering doc + skill, replacing the separate versioning and release-notes guides (#618)

## [1.2.0] - 2026-07-09

Agent Designer completion. Finishes the run-time binding trio (skills join model + tools), makes the chat input honor an active agent's governed bindings, adds a live side-by-side editor preview and model-parameter governance, brings full knowledge-base management into the Designer, and flips the `/agents` API on by default. The Agent Designer nav stays admin-gated Preview; no breaking changes and no migration.

### 🚀 Added

- Skill binding resolution at invocation: an Agent's bound skills now *replace* the request's skills and force `agent_type="skill"` for the turn, each re-checked against the invoking user (`AppRoleService.can_access_skill`) and flag-gated, with design-time validation — completing the model/tool/skill governance trio (tools shipped in 1.1.0). No binding ⇒ the request drives the turn as before (#602)
- Manage an agent's knowledge base from the Agent Designer: the assistant editor's KB section is extracted into a reusable `KnowledgeBaseSectionComponent` and reused in the agent form, replacing the read-only "managed automatically" card with the live document / web-crawl / connector flow — closing the last Agent migration blocker (frontend-only; the ingestion pipeline already keys on the record id and `agentId == assistantId`) (#608)
- Governed model parameters + live editor preview: `binding_validation._validate_model_params` rejects unsupported / locked / out-of-range params against the model's admin `supported_params` (author-facing 400 instead of a silent clamp); a data-driven Parameters subsection sits under the model picker; a new `AgentPreviewComponent` streams the *saved* agent through the real `/chat/stream` path so every binding resolves server-side (#609)
- Chat input reflects governed agent bindings: the model/tool/skill pickers lock to the active agent's bindings (locked read-only chips + "Set by agent" banners), resolved via `agentId == assistantId`. Per-primitive — an agent that binds only a model locks only the model; the rest stay free-select (#603)

### ✨ Improved

- When an agent locks the settings panel, it now shows only the bound tools/skills (`visibleTools` / `visibleSkills`) instead of the full accessible list with the unbound ones greyed out (#606)

### ⚠️ Changed

- The Agent Designer `/agents/*` API now defaults **on** (empty-string-safe kill switch — only the literal `false` disables), matching `scheduled_runs` / `memory_spaces`. SPA nav stays preview-gated (system-admin + "Preview" badge), so user-facing exposure is unchanged — the API just no longer 404s per-environment (#607)
- The Memory Spaces and Scheduled Runs side-nav entries are hidden for now while the Agent Designer is the focus. Their routes, pages, and capability probes are unchanged, so re-enabling is just restoring the nav template blocks (#611)

### 🐛 Fixed

- Chat-input picker locks are now released when starting a New chat: the release ran inside a guard that was false on the freshly-recreated session component, leaving the model/tool pickers stuck from the previous agent conversation (#603)
- The Agent Designer preview's model picker is locked to the agent's bound model (released on destroy) instead of showing the user's global model (#611)
- De-flaked the scheduled-runs cadence re-arm test by freezing the dispatcher clock; it was time-of-day dependent and failed when CI ran in the hour before 9am Boise (#608)

### 🏗️ Infrastructure

- CDK `config.agents.enabled` now defaults on (`!== 'false'` + `?? true` context fallback), mirroring `memorySpaces` / `scheduledRuns`. No new resources — the `AGENTS_API_ENABLED` env var only gates whether `/agents/*` responds (#607)

## [1.1.0] - 2026-07-08

Feature release adding four new capabilities — **Scheduled Runs** (unattended/proactive agent runs), **Memory Spaces** (bindable, shareable markdown memory), the **Agent Designer** (primitive-binding authoring surface), and **Knowledge Base Sync** (scheduled re-index of assistant sources) — plus a chat/session UX overhaul (per-conversation streaming, interrupted-turn persistence, mid-stream session titles). Every new surface is flag-gated; there are no breaking changes and no migration. Scheduled Runs, Memory Spaces, and KB Sync default **on**; the Agent Designer defaults **off**. The three preview surfaces (Agents, Memory Spaces, Scheduled Runs) are nav-gated to system-admins and carry a "Preview" badge.

### 🚀 Added

- **Scheduled Runs** — run an agent prompt unattended on a cadence (daily/weekday/weekly/interval) or on demand, delivered as a session with an unread indicator. New `/schedules` CRUD, `/runs/now`, and `/runs/grant`; gated by `SCHEDULED_RUNS_ENABLED` (default on) plus a new `scheduled-runs` RBAC capability (#558, #560, #561, #562, #563, #564, #565, #578)
- `run_agent_headless` entrypoint: per-owner Cognito bearer mint, server-side SSE drain, runtime session materialization, and an audit-only fail-closed governance floor (#560, #561)
- Revocable headless-grant record (create-on-enable from an attended session, 30-day login TTL) backing unattended run-as-user auth (#561)
- Interval cadence (every N minutes/hours) and "Run now" with app-wide background-task toasts (#578)
- Unread dot on scheduled-run deliveries and a Mark-as-read / Mark-as-unread toggle in the session menus (`POST /sessions/{id}/read` and `/unread`) (#572, #577)
- **Memory Spaces** — named, templated, shareable markdown "second brain" spaces with owner/editor/viewer sharing, loss-free `.zip` export, and a consolidation health pass. New `/memory/spaces` CRUD, `/shares`, `/export`, and `/consolidate`; gated by `MEMORY_SPACES_ENABLED` (default on) (#579, #582, #584, #585, #586, #589, #597)
- Space templates (Blank, Chief-of-Staff, Research-Notebook) and a SPA Memory panel — list/detail/create/share/export/delete (#582, #587)
- **Agent Designer** — an authoring surface that composes an Agent from RBAC-governed primitives (a governed single-select model + uniform `bindings[]` for tool / skill / knowledge_base / memory_space), evolving the assistant store in place. New `/agents/*` surface and `/agents/bindable` catalog; gated by `AGENTS_API_ENABLED` (default off). Legacy Assistants read as Agents via a compat mapping and `/assistants/*` is unchanged (#590, #591, #592, #598, #599)
- Run-time Agent binding resolution, re-checked against the *invoking* user's RBAC with block-on-missing: `modelConfig` override, bound Memory Space prompt injection, and `memory_*` tools (#594, #596, #601)
- **Knowledge Base Sync** — keep assistant KB sources (Google Drive files + web crawls) automatically re-indexed on a schedule (Daily/Weekly/Monthly), with pause/resume, run-now, and reconnect-on-reauth, surfaced as per-source controls on the assistant knowledge page. Gated by `KB_SYNC_ENABLED` (default on) (#542, #543, #544, #545, #546, #547)
- Per-conversation chat streaming: streaming state is now keyed per session, so concurrent conversations no longer cross streams; adds per-conversation scroll restore and a sidebar in-progress dot (#535)
- Interrupted-turn persistence: a stopped or dropped response now persists its partial text, context, and cost/usage metadata, with a reload chip to Continue (`POST /sessions/{id}/interrupt`) (#541, #548)
- Mid-stream session titles via a new `session_title` SSE event, with a shimmer skeleton while the title generates (#540)
- Session options menu (Rename / Share / Save / Delete) on the top-nav title, and the active assistant surfaced as a pill in the top nav (#538)

### ✨ Improved

- Conversation export now defaults to messages only — tool calls, images, and citations are opt-in (#537)
- OAuth call sites forward the connector's admin-configured `customParameters` verbatim, dropping hardcoded vendor baselines so the AgentCore vault key stays consistent across consent and retrieval (#550)
- KB-sync auto-sync control clarified: self-describing verbs, always-visible "Last synced", a "Saving…" indicator, unified skeleton loading, and web-source/document parity (#552, #555)
- Assistant editor groups the Knowledge Base section into a contrasted inset panel; added Google connector (Drive/Docs/Gmail/Calendar) logos (#557)

### 🐛 Fixed

- OAuth consent state is cleared on session switch so an "Authorization needed" banner can't leak onto another conversation (#539)
- Tool card is kept after an OAuth / tool-approval resume instead of being dropped when the resumed stream omits the original `tool_use` block (#532)
- User-sync timestamps normalized to strict ISO 8601 (single trailing `Z`), so admin "Last login" / "Created" dates no longer render as "Never" in Safari (#556)
- Schedule edits can now clear a schedule's assistant or tool restriction via explicit clear flags (a bare `null` was read as "leave unchanged") (#569)
- Client-supplied `enabled_tools` is intersected with the caller's RBAC on the headless paths (schedule create/update, Run now), so a scheduled run can't persist a tool outside the owner's role (#568)
- Scheduled-runs worker lean image made importable (missing `cryptography`/`cachetools`, top-level `agents`/`strands` imports on the delivery path) (#566, #567)

### 🏗️ Infrastructure

- New dedicated `memory-spaces` DynamoDB table (OwnerIndex + MemberIndex GSIs) and content-addressed S3 bucket via `MemorySpacesConstruct` (#582)
- New sparse GSIs: `DueScheduleIndex` and `HeadlessGrantUserIndex` on the sessions-metadata table (scheduled runs), and `DueSyncIndex` on the assistants table (KB sync) (#542, #561, #563)
- Two new lean Lambda images with EventBridge sweeps, both following the platform-as-bootstrap pattern (byte-stable stub + out-of-band image deploy): `Dockerfile.scheduled-runs` (dispatcher + worker, `rate(5m)`) and `Dockerfile.kb-sync` (dispatcher + worker, `rate(15m)`) (#543, #565)
- New feature flags forwarded through `platform.yml`, all empty-string-safe kill switches: `SCHEDULED_RUNS_ENABLED`, `MEMORY_SPACES_ENABLED`, `KB_SYNC_ENABLED` (default on) and `AGENTS_API_ENABLED` (default off) (#553, #554, #561, #593, #597)
- IAM: added `bedrock:ListFoundationModels` / `bedrock:GetFoundationModel` to the app-api task role (admin model list), and granted the kb-sync worker read on the vault's backing OAuth secrets (#549, #571)

### 🔧 CI/CD

- `backend.yml` gains build + API-driven code-deploy jobs for the kb-sync and scheduled-runs Lambda images; nightly image-scan and supply-chain pinning lists include both new Dockerfiles (#543, #565)

### 📦 Dependencies

- New pins in the lean Lambda images only (no core backend or frontend dependency changes): `beautifulsoup4` 4.13.5, `trafilatura` 2.0.0, `lxml` 6.1.1 (kb-sync web re-crawl); `cryptography` 48.0.1, `cachetools` 6.2.4 (scheduled-runs worker); `httpx` 0.28.1, `bedrock-agentcore` 1.9.1, `boto3` 1.43.9, `pydantic` 2.12.5 (shared image runtime)

## [1.0.4] - 2026-07-01

IAM hotfix restoring AgentCore Memory. Both the App API task role and the AgentCore Runtime execution role were missing `bedrock-agentcore:GetMemory`, which `get_memory_strategies()` needs to resolve strategy IDs — breaking the memory dashboard (empty results) and long-term recall (retrieval silently disabled). Ships via the platform (CDK) pipeline; no migration.

### 🐛 Fixed

- Memory dashboard (`GET /memory`, `/memory/preferences`, `/memory/facts`) returned empty lists with a 200: `get_memory_strategies()` → `bedrock-agentcore:GetMemory` was denied on the App API task role, so strategy discovery yielded no namespaces and retrieval was skipped (`app-api-iam-grants.ts`)
- Agent stopped recalling long-term memories: the runtime's `_discover_strategy_ids()` makes the same `GetMemory` call to build retrieval namespaces; the runtime execution role also lacked the action, disabling long-term retrieval while `CreateEvent` writes still succeeded (`inference-api-iam-roles.ts`)

### 🏗️ Infrastructure

- Added `bedrock-agentcore:GetMemory` to the `AgentCoreMemoryAccess` statement on both the App API Fargate task role (scoped to the memory ARN) and the AgentCore Runtime execution role (scoped to `memory/*`); no other actions changed. Validated against the AWS Service Authorization Reference (`GetMemory` = Read on the `memory` resource type)

## [1.0.3] - 2026-06-30

Maintenance patch: CI/CD pipeline cleanup, re-enabled path-scoped auto-deploys, and a dependency/CodeQL sweep. No application code or user-facing behavior changes; upgrade in place.

### 🔒 Security

- Removed unused imports flagged by CodeQL: `Optional` in `agents/main_agent/agent_types.py`, `ssm` in `app-api/app-api-environment.ts` (#526)

### 📦 Dependencies

- **backend:** `joserfc` 1.6.3 → 1.7.2 (`backend/uv.lock`) and 1.6.5 → 1.7.2 (`scripts/backup-data/uv.lock`); remediates Dependabot GHSA-wphv-vfrh-23q5 / CVE-2026-48990 (#526)

### 🔧 CI/CD

- Serialize `platform.yml` and `backend.yml` under one repo-global concurrency group (`deploy-<ref>`) so a CloudFormation deploy and the API-driven backend code deploys can't run concurrently and stomp the same ECS service / AgentCore Runtime / Lambda; frontend stays independent, `cancel-in-progress` stays false (#525)
- Re-enabled push-triggered, path-scoped deploys for platform, backend, and frontend (develop → development env, main → production) after being `workflow_dispatch`-only since v1.0.0; each trigger is scoped to its own surface (#524)
- Extracted duplicated test gates into a reusable `tests.yml`, wired into `ci`, `platform`, `backend`, `frontend-deploy`, and `nightly-deploy-pipeline`; render the reusable test-gate job names statically so skipped single-suite callers no longer show raw `${{ }}` expressions as labels (#524, #526)
- Pruned dead nightly tracks (AI coverage analysis, merge-validation) and orphaned scripts (`ai-coverage-analysis.py`, `promote-ecr-image.sh`); removed the dead `source-project-prefix` input (#524)
- `docs-deploy` now publishes from `main` (was `develop`); fork-gated `docs-deploy` and `release` so forks syncing `main` don't auto-publish or auto-create releases (#524)

## [1.0.2] - 2026-06-29

Second patch on the 1.0.0 single-stack architecture. Headlined by **restoring tool use in assistant chats** (reverting the 1.0.0 knowledge-base-only change), plus a CodeQL security-hardening sweep, remediation of 6 Dependabot alerts, and a nightly-pipeline fix. No migration; upgrade in place.

### ✨ Improved

- **Assistants can use tools again** — reverts the 1.0.0 knowledge-base-only restriction (#382). The inference API no longer forces `enabled_tools=[]` on assistant turns and the "Knowledge Base Grounding / no external tools" directive is removed from the system prompt, so the user's selected MCP/tools flow through to assistant chats again. KB context is still pre-stuffed into the user message and assistant instructions still apply. The editor preview now forwards the owner's enabled tools and renders tool-use cards, matching consumer chat. Assistant chats emit tool-use (and MCP-App) events again (#517)

### 🐛 Fixed

- Nightly coverage pipeline: `test-backend`, `test-frontend`, and `install-frontend` jobs failed with "No such file or directory" after the single-stack refactor removed `scripts/stack-app-api/` and `scripts/stack-frontend/`; repointed `nightly.yml` to the sanctioned `scripts/backend/test.sh`, `scripts/frontend/install.sh`, and `scripts/frontend/test.sh` (behavior preserved 1:1) (#518)

### 🔒 Security

- **HIGH `py/incomplete-url-substring-sanitization`** — `external_mcp_client` now parses the URL host (`urlparse`) and matches an anchored suffix instead of substring-checking the whole URL, so an AWS marker in a path/query/userinfo can no longer trick SigV4 signing into attaching IAM credentials to a non-AWS host (#521)
- **HIGH `js/regex/missing-regexp-anchor`** — `admin-tool.model` parses the host (`new URL`) and anchors the AWS-endpoint regexes (`$`), preventing spoofed-host matches (#521)
- **MEDIUM `py/log-injection`** (24 sites across 16 files) — new `apis.shared.security.scrub_log()` neutralizes CR/LF/control characters; applied to every flagged user-controlled log value (#521)
- **MEDIUM `actions/untrusted-checkout`** — added `persist-credentials: false` to all `inputs.ref` checkouts in `nightly-deploy-pipeline.yml` (#521)
- **WARNING `py/regex/duplicate-in-character-class`** — removed a stray `[` from a `re.VERBOSE` comment that the parser misread as a character class (#521)
- Added regression tests: `TestAwsUrlHostSanitization`, `admin-tool.model.spec.ts` (13 cases), `test_log_sanitize.py` (#521)

### 📦 Dependencies

- **docs-site:** `astro` 6.3.1 → 6.4.8 (reflected XSS via slot name GHSA-8hv8-536x-4wqp, host-header SSRF GHSA-2pvr-wf23-7pc7, spread-attribute XSS GHSA-jrpj-wcv7-9fh9); `esbuild` → 0.28.1 via overrides (dev-server arbitrary file read GHSA-g7r4-m6w7-qqqr)
- **frontend:** `esbuild` → 0.28.1 via overrides (transitive through `@angular/build` 21.2.16; GHSA-g7r4-m6w7-qqqr)
- **backend:** `pydantic-settings` 2.13.1 → 2.14.2 (transitive; `NestedSecretsSettingsSource` symlink traversal / local file read GHSA-4xgf-cpjx-pc3j) (#520)

## [1.0.1] - 2026-06-26

First patch on top of the 1.0.0 general-availability release. Adds the ability to **save a conversation to a connected app** ("Save to…", with Google Drive as the reference export target) and **support for external (cross-account) Route53 hosted zones**. Both additions are additive and off by default until configured; existing 1.0.0 deployments upgrade in place with no migration.

### 🚀 Added

- **Save conversations to connected apps ("Save to…")** — push a full conversation transcript out to a connected app as a native Google Doc, Markdown, or plain-text file. New `apis.app_api.export_targets` package: `ExportTargetAdapter` contract + code-shipped registry mirroring the read-side `FileSourceAdapter` pattern, a `GoogleDriveAdapter` reference target, and a transcript renderer. User routes `GET /export-targets` (catalog with per-connector `connected`/`supportedFormats`/`browsable`) and `POST /sessions/{id}/export` (renders + creates the document via the user's own AgentCore Identity token; `409`→consent, `503`→workload misconfig). Admin `GET /admin/export-target-adapters` + new `OAuthProvider.export_target_adapter_id` mapping; `ExportReceipt` persisted to session metadata. SPA `ExportDialogComponent` + `ExportService`, a "Save to…" action on the conversation list, and an admin connector-form adapter dropdown (#507, #508, #509, #510, #511)
- **External (cross-account) Route53 hosted zones** — new optional `manageDnsRecords` flag (env `CDK_MANAGE_DNS_RECORDS`, context `manageDnsRecords`; defaults to `true`). When `false`, the SPA, ALB, artifacts, and mcp-sandbox origins still attach their custom domain + ACM cert but skip the in-account `HostedZone.fromLookup` + ALIAS/A record creation (which fails when the zone is in another account), emitting `CfnOutput` record-name/alias-target pairs per origin so an operator can create the records by hand. Plumbed through `load-env.sh`, the `platform.yml`/`teardown.yml`/`nightly-deploy-pipeline.yml` workflows, and the deployment docs (#512)

### 🧪 Test coverage

- 1,400+ lines of new export-target tests: `test_export_routes.py`, `test_export_target_service.py`, `test_export_google_drive.py`, `test_export_render.py`, `test_export_target_adapters_admin.py` (backend); `export-dialog.component.spec.ts`, `export.service.spec.ts` (frontend)

## [1.0.0] - 2026-06-24

The **1.0.0 general-availability release** — the platform graduates from beta to a stable, single-stack architecture. The CDK app collapses from nine CloudFormation stacks into one `PlatformStack` with a platform-as-bootstrap code-deploy model; admin-curated Conversation Modes, external file-source connectors and website crawling for assistant knowledge bases, self-service AgentCore Gateway MCP target registration, a curated model catalog with the new Amazon Bedrock Mantle provider, per-turn context attribution, a Starlight documentation site, and a full backup/restore DR toolchain all ship; plus a coordinated security-hardening sweep and remediation of all 22 HIGH Dependabot findings.

### 🚀 Added

- **Per-tool MCP enablement** — scoped tool ids (`toolId::name`) selecting a single tool of an MCP server, with live `POST /admin/tools/{id}/discover` and `POST /tools/{id}/discover` endpoints (#469)
- **Conversation Modes** — admin-curated catalog of custom system prompts (e.g. "Guided Learning", "Concise") that users opt into per conversation; appended to the base system prompt at invocation. Admin CRUD `/admin/system-prompts` + user read `/system-prompts` (name/description only); new `system-prompts` DynamoDB table. Ships enabled (#411)
- **File-source connectors** — import knowledge-base documents from external OAuth providers. Provider-agnostic `FileSourceAdapter` framework + registry with a shipped `GoogleDriveAdapter`; `GET /file-sources`, `GET /connectors/{id}/roots|browse|search`, `POST /assistants/{id}/documents/import` (202); admin connector→adapter mapping (`OAuthProvider.file_source_adapter_id`) + `GET /admin/file-source-adapters`; `Document` provenance fields; SPA `FileSourceBrowserDialogComponent` (#366, #367, #371, #372)
- **Web-sources crawling** — crawl websites into an assistant's knowledge base via `POST /assistants/{id}/web-sources/crawl` + crawl-status endpoints. Robots-respecting, SSRF-guarded, same-domain bounded-BFS crawler (5 MB/page, 15-min budget) with trafilatura→markdown extraction into the documents bucket for the existing ingestion Lambda; `WebSourceDialogComponent` with live discovery polling (#378)
- **Assistant viewer/editor share permissions** — per-user permission levels on shared assistants (editors can edit settings/docs/test-chat but not delete, change visibility, or manage shares); `AssistantSharesResponse.sharedWith` becomes `ShareEntry[]`; `PATCH /assistants/{id}/shares`; per-row "Can view / Can edit" UI + "Editor" badges (#113, #383, #384)
- **Download uploaded assistant documents** from the editor for `complete` docs (#380)
- **Gateway MCP self-service targets** — admins register an externally deployed MCP server as a target on the shared AgentCore Gateway from the admin Tools form: `MCPGatewayConfig` model, `GatewayTargetService` + admin route lifecycle (create-AWS-first / update-reconcile / delete with 409/502 mapping), `GET /admin/tools/{tool_id}/gateway-status`, `protocol=mcp` admin form with Discover-from-server, a `NONE` (public-endpoint) credential type, per-target `lambda:InvokeFunctionUrl` grant/revoke, and runtime catalog-tool expansion to `gateway_<target>___<tool>` ids (#419, #450, #452, #453, #455, #456, #457)
- **Curated model catalog** — one-click add of fully-configured Bedrock models (Claude Haiku/Sonnet/Opus 4.x) with pricing, modalities, per-param specs, role-picker dialog, "Preview & customize" prefill, and per-card light/dark provider logos (#393)
- **Amazon Bedrock Mantle provider** — AWS's OpenAI-compatible surface for open-weight models (qwen, gpt-oss, gemma, deepseek) via a SigV4-presigned bearer token over the OpenAI wire protocol; `GET /admin/mantle/models` browse endpoint (#479)
- **Per-turn context attribution** — native Bedrock `CountTokens` decomposes aggregate `inputTokens` into system / tools / messages partitions, streamed over SSE as `contextBreakdown` and rendered as a "Context: <total>" badge on assistant messages (#428, #430, #431, #433)
- **MCP Apps: refresh-survival** — model-initiated UI resources persist as gzipped HTML in the sessions-metadata table and replay into the messages response so `<mcp-app-frame>` survives a page refresh (#413)
- **MCP Apps: progressive rendering (SEP-1865)** — the App frame mounts early at `content_block_start` and forwards `ui/notifications/tool-input-partial`, so Apps that animate from streaming arguments work end-to-end (#417)
- **MCP Apps: fullscreen display mode** with a promoted title-bar header and reachable consent (#409, #410, #418)
- **Backup/restore DR toolchain** — `scripts/restore-data/` + `restore-data.yml` replay a `manifest.json` snapshot (DynamoDB exports, S3 sync, Cognito IdPs/users/groups, S3 Vectors index) into a deployed `PlatformStack`; SSM-resolved targets, idempotent, `--dry-run`, `skip_cognito_users` (#396, #421, #422, #423, #425, #481)
- **Reproducible dev container** — `.devcontainer/Dockerfile` with every toolchain pinned by sha256/PGP (Python 3.13 / uv 0.7.12, Node 22 / npm 11.2.0, AWS CLI 2.34.40, Docker CLI 29.4.3, CDK CLI, Playwright chromium) (#391)
- **Teardown workflow** — guarded (`DESTROY` confirmation, `workflow_dispatch`-only) full-environment teardown via `cloudformation delete-stack` covering single-stack and legacy multi-stack deployments (#392)
- **Starlight documentation site** under `docs-site/`, deployed to GitHub Pages: Introduction, Local Development, full Deployment section, Architecture Overview with an AWS diagram, Configuration, Features, and an Admin section mirroring the SPA console + an API Keys page; frosted-glass brand theme; standalone `/maintenance` splash (#432, #440, #441, #442, #444, #445, #459, #482, #483)
- **Forward admin OIDC token on MCP tool discovery** via a `forward_auth_token` flag for same-team `AuthType=NONE` Lambda-URL MCP servers (#498)
- **Time-of-message info on hover** over user messages (#5a7180c6)

### ✨ Improved

- Assistant editor + file-connector UX redesigned to the `rounded-2xl` list/form language; connectors surfaced as buttons above the drop zone; knowledge-base "add" groups collapsed into a single inline action row with skeleton chips; OAuth consent started in place from the connector button (#377, #379, #06ef6673)
- Assistant-editor preview tailors chat-input controls (hides voice/settings, exposes file attachments via `file_upload_ids`) (#381)
- `GET /tools/` surfaces each MCP server's individual tools via `UserToolAccess.serverTools` with effective per-tool enabled state (#469)
- Model-settings slide-over and model create/edit form restyled to the canonical admin list/form design tokens, with an edit-mode loading spinner instead of an empty-form flash (#387, #395)
- MCP Apps widget-initiated `ui/message` turns now get the loading indicator and scroll-to-last-user affordances the composer path already had (#505)

### ⚠️ Changed

- **Single-stack CDK architecture (breaking for multi-stack fork migrators).** The nine-stack CDK app collapses into one `${prefix}-PlatformStack`. All per-component CDK feature flags removed (`CDK_FRONTEND_ENABLED`, `CDK_APP_API_ENABLED`, `CDK_INFERENCE_API_ENABLED`, `CDK_GATEWAY_ENABLED`, `CDK_FILE_UPLOAD_ENABLED`, `CDK_ASSISTANTS_ENABLED`, `CDK_RAG_ENABLED`, `CDK_FINE_TUNING_ENABLED`, `CDK_ARTIFACTS_ENABLED`, `CDK_MCP_SANDBOX_ENABLED`) — deploy-everything-always. Backend code now ships out-of-band via AWS APIs, not CFN. Migration documented in `.github/docs/deploy/upgrade-from-multi-stack.md` (#396, #434)
- **SSM `image-tag` contract (breaking for multi-stack fork migrators).** `/{prefix}/{app-api,inference-api,rag-ingestion}/image-tag` changed from a bare tag/short-SHA to a FULL ECR URI; a stale legacy value fails the first `PlatformStack` deploy on CFN pattern-validation. The seed script auto-repairs (#420)
- **Assistant consumer chat is knowledge-base-grounded with zero external tools** — enforced at the inference-API chokepoint (`enabled_tools=[]`) plus a "## Knowledge Base Grounding" system-prompt section. Side effect: no MCP-App `ui_resource` events for assistant chats (#382)
- `analyze_spreadsheet` hard-fails downloads over 25 MB (soft-warns at 10 MB), tunable via `ANALYZE_MAX_FILE_SIZE_BYTES` / `ANALYZE_WARN_FILE_SIZE_BYTES`; checked before S3 GetObject (#397)

### 🐛 Fixed

- File-source calls send the `OAuth2CallbackUrl` header, fixing `CallbackUrlUnavailableError` (503) immediately after a successful connect (#373)
- File-source token resolution uses consent-matched `customParameters` (`force_authentication=True`), fixing spurious 409 "not connected" for connected connectors (#374)
- Gateway `mcpServer` IAM targets require an explicit `iamCredentialProvider`; bare `GATEWAY_IAM_ROLE` was rejected. Agent Gateway client repointed from a hardcoded SSM param to the CDK `/{prefix}/gateway/id` so admin-registered targets reach the agent (#457)
- Managed-models list "ghosting" — stored models with a whole-number float `thinking.default` (DynamoDB Decimal roundtrip) failed validation on read and were silently skipped; validator now accepts them; adds a delete-confirmation modal + list loading state (#394)
- MCP Apps: inner iframe collapsed to the 150px replaced-element default (CSSOM 100%-height chain); fullscreen overlay rendered behind chrome / mis-sized (`z-index:9999` fixed iframe; entry-animation `transform` no longer traps the fixed overlay); `<meta>`-vs-header CSP mismatch blocked `eval` Apps; `ui/message` rejected spec-compliant content arrays; a single transient TLS blip on MCP client start failed the whole agent build (now retried 3×) (#409, #410, #412, #414, #503, #504)
- File-upload duplicate-document-name error misclassified as a "file too large" error (#403)
- Restore: base64-decode `B`/`BS` (and nested `L`/`M`) attribute values before `TypeDeserializer` (crashed at sessions-metadata); cross-pool federated-user migration uses deterministic `migrated-<sub>` usernames + re-attaches restored IdPs to the BFF client; `boto3` `max_pool_connections` 10→32 for the 16-worker pool; S3 Vectors index included so restored knowledge bases retrieve hits (#422, #425, #423, #481)
- Build arm64 images on native ARM runners — `rag-ingestion` was built amd64 against an arm64 Lambda (`Runtime.InvalidEntrypoint`, uploads stuck with no embeddings) (#496)
- Restore stable IAM role names for AgentCore execution roles — auto-generated names force-replaced the create-only `executionRoleArn` into `UPDATE_ROLLBACK` (#495)
- Re-deploy artifact-render code when the live Lambda drifts from what we shipped (CDK bootstrap 503 stub was serving `artifacts.{domain}`) (#438)
- Restore the MCP-sandbox cert deploy var lost in the stack consolidation (NXDOMAIN → App `postMessage` origin mismatch) + synth-time guard (#434)
- Nightly/teardown reconciled with single-stack (delete `${prefix}-PlatformStack` via `delete-stack`; `always()` ephemeral auto-teardown so failed deploys never leak billable resources) (#499, #500)
- App-api granted `secretsmanager:PutSecretValue` (scoped to the auth-provider secret) so auth-provider config stops failing `AccessDenied`; Cognito `CreateGroup`/`AdminAddUserToGroup`/`AdminDeleteUser` for first-boot + rollback (#501, #494)
- Admin MCP tool discovery forwards the admin OIDC token (task role lacks `lambda:InvokeFunctionUrl`, so SigV4 discovery 403→502 for `AuthType=NONE` Lambda-URL servers) (#498)

### 🔒 Security

- New shared `apis.shared.security` package adopted app-wide: `url_validator.validate_external_url` (DNS-rebinding-safe SSRF guard rejecting loopback / link-local / RFC1918 / ULA / multicast / reserved / CGNAT + cloud-metadata), `ownership` helpers (404-not-403 to remove the enumeration oracle), and AWS-client error-mapping handlers (#443)
- `fetch_url_content` routed through the URL validator with manual redirect-chain validation (`follow_redirects=False`, ≤3 hops, each `Location` re-validated) (#f1cb0ae2)
- Outbound MCP SigV4 signing scoped to recognized AWS endpoints only — unrecognized hosts are refused instead of receiving task IAM credentials (#8819aefe)
- Static AST policy gates user-supplied diagram/analysis code sent to Code Interpreter to a plotting/data-analysis allowlist (bans subprocess/os/sys/socket/eval/exec/dunder) (#0e043730)
- Session-metadata `PUT` rejects (404) when the session id is owned by another user, closing a create-on-not-found enumeration oracle (#a4784556)
- User-supplied system prompts wrapped in a `PLATFORM_SAFETY_FLOOR` inside non-escapable `<user_instructions>` tags (#b089d564)
- Profile-sync hardening: persisted email and roles bound exclusively to the validated session/JWT (`current_user.*`), no longer influenced by the request body (#12defcfc, #458)
- Role-mapping validation (`jwt_role_mappings` regex `^[A-Za-z0-9_-]{2,64}$`, map-everyone tokens banned on `system_admin`) + a monotonic roles-version cache-invalidation counter (#cf613b15)
- Admin error-path sanitization (no env-var-name or input echo on errors); viewer-facing CloudFront + ALB pinned to a TLS 1.2+ minimum baseline; dedicated SSRF and cursor-validation test suites added (#7cb9047c, #484)

### ⚡ Performance

- Re-enabled Strands Bedrock auto prompt caching (`CacheConfig(strategy="auto")`), now safe after the upstream cachePoint/document-attachment collision was resolved in strands-agents 1.39.0 (#471)

### 🏗️ Infrastructure

- `PlatformStack` composes ~39 single-responsibility constructs under `infrastructure/lib/constructs/`; built in two phases (constructor + `wireCompute()`), eliminating every cross-stack `Fn::ImportValue` and deploy-ordering dependency (#396)
- Platform-as-bootstrap: CDK ships byte-stable placeholder assets from `infrastructure/bootstrap-assets/{app-api,inference-api,rag-ingestion,artifact-render}/`; real code ships via `aws ecs register-task-definition`+`update-service`, `aws bedrock-agentcore-control update-agent-runtime`, and `aws lambda update-function-code` (#396)
- Content-hash Docker build pipeline under `scripts/build/` (ECR-tag hit ⇒ skip rebuild) (#396)
- Shared CloudFront wildcard cert — new top-level `CDK_CLOUDFRONT_CERTIFICATE_ARN`; frontend / artifacts / mcp-sandbox fall back to it (section-specific wins); one `us-east-1` `{domain}` + `*.{domain}` cert with cert-missing guards (#491)
- AgentCore runtime execution role granted `bedrock:CountTokens` (context-attribution foundation) (#428)
- `/{prefix}/gateway/id` SSM publication + app-api Gateway-target IAM grants (`bedrock-agentcore:{Create,Get,Update,Delete,List}GatewayTarget` scoped to `gateway/*`) (#452)
- New `system-prompts` DynamoDB table (Conversation Modes; app-api CRUD, inference-api `GetItem` only) (#411)
- Restored ~22 SSM parameters (17 table, 4 bucket, `/inference-api/memory-id`) that the stack consolidation dropped and the restore tooling needs (#421)

### 📦 Dependencies

- Backend: `cryptography` 47.0.0 → 48.0.1, `starlette` 1.0.0 → 1.3.1, `python-multipart` 0.0.27 → 0.0.31, `pyjwt[crypto]` 2.12.1 → 2.13.0, `urllib3` pinned 2.7.0, `aiohttp` 3.13.5 → 3.14.1, `authlib` 1.7.0 → 1.7.1, `idna` pinned 3.15; new `beautifulsoup4` 4.13.5, `trafilatura` 2.0.0 (web-sources) (#487, #488, #378)
- Frontend: `@angular/*` 21.2.11 → 21.2.17, `@angular/cdk` 21.2.9 → 21.2.14, `@angular/build`/`cli` 21.2.9 → 21.2.16, `mermaid` 11.14.0 → 11.15.0; overrides `hono` ≥4.12.25, `undici` ≥7.28.0, `vite` ≥8.0.16, `piscina` ≥5.2.0, `@babel/core` bounded 7.29.7 (#487, #488)
- Infra: `aws-cdk-lib` 2.251.0 → 2.260.0, `aws-cdk` CLI 2.1120.0 → 2.1128.0 (#492)
- Remediates all 22 HIGH Dependabot findings plus easy MEDIUM/LOW (same set merged across #487, #488, #489)

### 🔧 CI/CD

- Deploy workflows (`platform.yml`, `backend.yml`, `frontend-deploy.yml`) gated to `workflow_dispatch`-only for the v1.0.0 release — `push:` triggers commented out so syncing/forking the codebase never auto-deploys into a user's AWS account; re-enable by uncommenting
- New `platform.yml` (CDK), `backend.yml` (build → API deploy), and `frontend-deploy.yml` workflows; `nightly-deploy-pipeline` rewritten platform → backend → frontend; legacy per-stack workflows/scripts/tests deleted (#396)
- New `ci.yml` pull-request test gate (backend pytest / frontend vitest / infra jest) on PRs into `develop`/`main`; deploys never run on PRs (#490)
- New `docs-deploy.yml` builds the Starlight site and publishes to GitHub Pages (#432)
- `aws-cdk` CLI pinned 2.1128.0 + Node 22 pinned in deploy jobs (#492); `Backend Stack` workflow renamed to `Backend Deploy` (#423); stale `6.` prefix dropped from the Seed Bootstrap Data workflow
- `CDK_ARTIFACTS_EXTRA_FRAME_ANCESTORS` plumbed through platform/nightly deploy workflows (#485)

### 📚 Docs

- New `.github/docs/deploy/upgrade-from-multi-stack.md` (legacy SSM cleanup, teardown); `ACTIONS-REFERENCE.md` config table reflects the single `PlatformStack`; deploy guides document `CDK_MCP_SANDBOX_CERTIFICATE_ARN`; corrected a stale SSM comment on mcp-sandbox origin wiring; devcontainer docker-GID gotcha documented in `dev-environment.md` (#396, #436, #437, #502, #391)

## [1.0.0-beta.27] - 2026-05-20

The largest release since the BFF cutover. Two new user-facing surfaces (Artifacts and MCP Apps host-renderer) each backed by a new CDK stack, an admin shell redesign that replaces the 15-card grid with a persistent grouped sidebar, recoverable `max_tokens` truncation with a Continue affordance, model-aware adaptive thinking for Opus 4.7, an inference-API `/ping` reaper fix, and a pre-migration backup tool. `bedrock-agentcore` 1.6.4 → 1.9.1, `boto3` 1.42.96 → 1.43.9, `strands-agents` 1.39.0 → 1.40.0.

### 🚀 Added

- **Artifacts feature** — agent-authored versioned standalone documents (HTML, Markdown, code) that render in a sandboxed iframe in a docked side panel. Backed by a new `ArtifactsStack` (DDB `user-artifacts` heads + version log with session GSI; private S3 `artifacts-content` bucket; render Lambda; CloudFront on `artifacts.{domain}`) and short-lived HMAC-signed render-token JWTs minted by app-api. Two new built-in tools (`create_artifact`, `update_artifact`) registered as default public tools so the feature works on first deploy. Versions are immutable (no `s3:DeleteObject` on inference-api). HTML mode allows scripts from `cdn.tailwindcss.com`, `esm.sh`, `cdn.jsdelivr.net`, `unpkg.com`; `connect-src 'none'`. Markdown mode wraps GFM input in a self-contained HTML render harness server-side. Frontend: docked resizable panel, auto-open on first creation, skeleton loader, latest-version on update, per-version history cards, preview/code toggle with syntax-highlighted source view, download button (#306, #309, #310, #311, #312, #314, #316, #317, #318, #319, #321, #322, #323, #324, #325, #326, #334)
- **MCP Apps host-renderer** — third-party MCP servers can ship UI alongside their tools. New `McpSandboxStack` (CloudFront on `mcp-sandbox.{domain}` with a CloudFront Function emitting per-resource `frame-ancestors` CSP; outer mount-page S3 bucket). Agent advertises `experimental.ui` on MCP `initialize`, fetches `ui_resource` payloads via `resources/read`, emits a `ui_resource` SSE event with `uri`, `permissions`, and `sandboxOrigin`. Frontend `<mcp-app-frame>` Angular custom element renders Apps in a sandboxed iframe with a `postMessage` bridge that enforces allowed message types (`ui/message`, `ui/update-model-context`) and origin checks. App-initiated `tools/call` proxied through app-api over an event broker. Explicit user consent prompt on first frame, persisted across reloads via card store. Default-on this release (`Defaults.MCP_APPS_HOST_ENABLED` flips false → true) with `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` wired into inference-api runtime env from SSM. Tools whose only output is a `ui_resource` are filtered out for non-capable clients. Committed `budget-allocator-server` example; runbooks updated (#296, #339, #342, #343, #344, #345, #346, #347, #348, #349, #352, #353, #355, #360)
- **Admin shell redesign** — persistent grouped sidebar nav (Usage & Spend / AI Configuration / Identity & Access / Customization) replaces the 15-card admin grid. `/admin` redirects to `/admin/costs`. Quotas (Tiers / Assignments / Overrides / Inspector / Events) collapses 5 sibling routes into a single tabbed page; Fine-Tuning (Access / Costs) collapses into one. "Back to Admin" link removed from 10 sub-pages. Cost summary cards restructured (title on its own row, icon as top-right corner accent) so "Cache Savings" / "Avg Cost/User" stop wrapping (#300)
- **Compact model browse + manage views** — manage-models and the Bedrock/Gemini/OpenAI browse pages redesigned as one-line scannable rows with expand-on-demand detail; slim inline filter toolbar; inline enable/disable toggle so status changes don't require opening the form; `rounded-2xl` matches the chat input (#332)
- **Compact tool catalog + form** — same redesign applied to admin tools list and create/edit form. Compact expandable rows; form flattened to shared list-page token set (`rounded-2xl`, `text-sm/6`, `text-2xl/8` header, `focus:ring-2`); no behavior changes (#335)
- **Admin-managed user-menu links** — new admin domain so org admins can curate the SPA user-menu links without code changes. Each link is either an external URL (new tab) or an in-app modal with admin-authored Markdown. New `user-menu-links` DDB table; admin CRUD at `/admin/user-menu-links` (`require_admin`); public enabled-only read at `/user-menu-links` (cookie-aware `get_current_user_from_session`) (#298)
- **Recoverable `max_tokens` truncation** — `MaxTokensReachedException` is classified specifically in the stream processor and emits a `max_tokens`-coded recoverable `stream_error` event. Continue is a resume, not a new turn: `continue_truncated` re-enters the agent loop with an empty-list prompt (assistant-prefill) bypassing quota / RAG / file-resolution. `lastTurnContinuable` marker on session metadata flows through `SessionMetadataResponse` so Continue reappears after a refresh. Frontend renders a compact inline "Response length limit reached" notice + Continue button (no verbose error bubble); continuation-aware message-map sync pins the partial and appends the continuation. `stream_error` is now an always-allowed parser event (#328)
- **Model-aware adaptive thinking + `effort` knob** — `_shape_thinking_value` is now model-aware. Opus 4.6/4.7, Sonnet 4.6, and Mythos emit `{type: "adaptive", display: "summarized"}` (the explicit `display` keeps the reasoning trace visible — Opus 4.7 defaults `display` to `"omitted"`); older models keep `{type: "enabled", budget_tokens: N}`. New `effort` canonical inference param wired through `additional_request_fields.output_config.effort` (NOT `additionalModelRequestFields`). Wired through the admin model form and the user-facing chat settings panel as a new select control with server-side allowed-set gating. Generic `allowed` enum on `ModelParamSpec` so the per-model effort-tier difference (Sonnet 4.6 vs Opus 4.7) is data, not a model-family branch (#331)
- **Pre-migration backup tool** — `scripts/backup-data/` produces a complete restore-friendly snapshot for a given `CDK_PROJECT_PREFIX`: all ~20 application DDB tables via `ExportTableToPointInTime`, user-content S3 buckets via `aws s3 sync`, full Cognito user pool config including identity providers and app clients with plaintext client secrets preserved, users / groups / group memberships, and best-effort AgentCore Memory events. Each run lands in a freshly-created versioned SSE-encrypted TLS-only `{prefix}-backup-{utc_timestamp}` bucket. `manifest.json` is the single source of truth for restore. Cognito password hashes are not exportable by AWS — documented prominently. Ephemeral session/state tables excluded by default. `workflow_dispatch` GitHub workflow wired via the existing OIDC composite action (#361)
- **Live tool output streamed into the tool rail** during artifact authoring (#316)
- **Markdown content-type support** in the artifact tool (#318)
- **Configurable extra CSP `frame-ancestors`** for the artifact origin (#314)
- **`<mcp-app-frame>` custom element + `postMessage` bridge** with origin- and type-enforcement (#346)
- **Tool result renderer registry** — signal-backed `ToolRendererRegistryService` keyed by tool name replaces the implicit text/JSON/image switch baked into `ToolUseComponent`. The default renderer reproduces the prior markup verbatim — zero visible change. `calculator`, `fetch_url_content`, and `create_visualization` migrated as proof points. Foundation for the MCP Apps `<mcp-app-frame>` renderer (#339)
- **Copy-to-clipboard button on chat code blocks** + Prism syntax-highlighting bundles for JavaScript, TypeScript, Python, and SQL alongside the existing C#/CSS bundles (#299)
- **Autofocus chat input on session load and switch** so the user can type immediately without clicking. Assistant-preview empty state opts out via a new `autoFocus` input (#333)
- **Denser session sidebar with skeleton + entry animation** — rows tighten from ~40px to ~32px (`py-2 → py-1.5`, `text-sm/6 → text-sm/5`); nested flex wrappers around the title removed; group gaps tightened. A 10-session list is ~25% shorter overall. Inactive items `font-normal`; active row `!font-medium` via `routerLinkActive` (#301)

### ✨ Improved

- **Spinners across admin / settings / fine-tuning / auth pages** — 24 loading spinners had been rendering as a uniform gray ring in dark mode (no visible motion); they now spin with the proper accent (#300)
- **Admin shell wider with sidebar label wrapping fixed** (#305)
- **User-menu links / in-app modals visually distinguished** in both modal preview and runtime rendering (#303)
- **`mcp-sandbox` outer CSP + inner mount aligned** with the upstream `ext-apps` basic-host reference; blob iframe rendering, first-class block element, Angular 21-specific fixes (#352, #353)
- **Dynamic per-resource CSP** for the sandbox proxy — CloudFront Function decodes a URL-encoded `?csp=` query param scoped to one resource and emits the per-request `Content-Security-Policy` header. Source loaded from `assets/mcp-sandbox/csp-function.js` with `frame-ancestors` JSON-injected at synth; substitution asserts the placeholder is present exactly once so a future refactor that loses it fails loudly at synth (#355)

### 🐛 Fixed

- **Critical:** `MaxTokensReachedException` surfaced as a generic leaky error (`...unrecoverable state... https://strandsagents.com/...`) and the only "recovery" re-sent the original prompt as a new user turn, so the model re-answered from scratch and re-truncated — an infinite loop. Continue is now a true resume (`continue_truncated` empty-list prompt, assistant-prefill on restored history) bypassing quota / RAG / file-resolution like the existing interrupt-resume path (#328)
- **Opus 4.7 400 on `thinking.type="enabled"`** — Opus 4.7 rejects the legacy thinking shape; model-aware `_shape_thinking_value` now emits `{type: "adaptive"}` for Opus 4.6/4.7, Sonnet 4.6, Mythos. Without this fix, Opus 4.7 turns failed at the SDK boundary (#331)
- **Float-typed `max_tokens` / `top_k` crashed boto3's Bedrock Converse client.** Untyped inference params (`Dict[str, Any]` from JSON) let a float reach the SDK, which rejects a float `maxTokens` with a hard validation error. Coerced to `int` at the single provider-translation chokepoint (covers fresh + resumed turns, all providers). The thinking-vs-`max_tokens` consistency guard previously used `isinstance(..., int)` and silently no-opped on float input; it now coerces first so an inconsistent request (`thinking >= max_tokens`) is rejected before reaching Anthropic. Model-ceiling cap protects against admin-configured `max_tokens` exceeding the model's hard limit (#329, #330)
- **Silent mid-stream microVM reaping on long generations.** AgentCore's idle reaper requires an integer `time_of_last_update` field alongside `status`; when absent, the platform reaps the microVM at `idleRuntimeSessionTimeout` regardless of reported status (`bedrock-agentcore-sdk-python#471`). Inference-api's `/ping` now emits a fresh timestamp on every call as the documented mitigation. Status casing also corrected to match `PingStatus`. Workaround until async-task busy tracking lands and we can report `HealthyBusy` (#338)
- **Frontend deploy bundles shipped the `'dev'` placeholder.** `scripts/stack-frontend/build.sh` invoked `ng build` directly, bypassing the npm `prebuild` lifecycle hook that runs `gen-version.js`. The user menu rendered "local" on `develop` and `main`. Build script now runs `gen-version.js` explicitly before the build (#336)
- **Chart.js artifacts loaded via `cdn.jsdelivr.net` rendered blank.** The artifact-origin CSP only permitted scripts from `cdn.tailwindcss.com` and `esm.sh`. Widened script-src to `cdn.jsdelivr.net` and `unpkg.com`, kept byte-identical across the render Lambda `CSP_SCRIPT_SRC` env var and the system-prompt allowlist (#326)
- **Admin user-menu-links resource fired a duplicate load request for non-admin users** — gated to admin-only (#315)
- **Artifact card z-index escapes its message row on focus** — scoped with `isolation: isolate` (#323)
- **`mcp-sandbox` CFN `Comment` overflowed AWS's 128-char cap** — twice, on the original RHP and the rebuild (#356, #357)
- **`mcp-sandbox` CSP not URL-decoded in CloudFront Function** — decoded properly; `x-csp-debug` diagnostic header added during the investigation (#358) and removed once the fix landed (#359)
- **Inner App iframe gained `allow-same-origin`** to match the upstream basic-host reference (#360)
- **Docker build hard-fail from rotated `curl` apt pin.** Debian rotated `curl 8.14.1-2+deb13u2` out of the trixie apt index (superseded by `+deb13u3`); the exact pin made every App API / Inference API Docker build on `develop` fail with `E: Version '8.14.1-2+deb13u2' for 'curl' was not found`. Pin bumped (#327)
- **Artifact env vars not passed to non-`ArtifactsStack` consumer workflows.** `validateConfig` runs on every stack synth (the `bin/` instantiates all enabled stacks), so consumer workflows need to pass `CDK_HOSTED_ZONE_DOMAIN`, `CDK_ARTIFACTS_ENABLED`, and `CDK_ARTIFACTS_CERTIFICATE_ARN` even though they don't synth `ArtifactsStack` directly. Five deploys failed on the develop merge before this fix (#307)
- **`infrastructure-stack` tests asserted a stale DDB count.** `resourceCountIs(18)` went red when `user-menu-links` landed (19 tables). Replaced the magic number with an enumerated, justified table list (#350)

### 🔒 Security

- **Artifacts isolation.** `artifacts.{domain}` is a different cookie-jar host from the SPA. CSP `connect-src 'none'` — artifacts cannot make outbound network calls. Render-token JWTs are scoped to one `(artifact_id, version)` and are HMAC-signed with a Secrets-Manager-managed key. S3 versions are immutable: there's no `s3:DeleteObject` grant on the inference-api role
- **MCP Apps isolation.** `mcp-sandbox.{domain}` is a separate origin from the SPA. Per-resource `frame-ancestors` CSP is emitted by a CloudFront Function on viewer-response. Inner App iframe carries `allow-same-origin` to match the basic-host reference. Explicit user consent (with reload persistence) gates first-time framing
- **Dead Bearer-only auth removed from app-api (#297).** A sweep of `app_api/` for `Depends(get_current_user)`, `Depends(security)`, `Depends(verify_token)`, and manual `Authorization` header reads turned up exactly two routes still on Bearer auth, both in `chat/routes.py`. Dead Bearer paths removed; `POST /chat/agent-stream` is documented as intentionally Bearer for non-SPA callers (API-key tooling, scripts). All other app-api routes are cookie-based BFF auth post-beta.24

### ⚠️ Breaking changes

- **MCP Apps default-on.** `Defaults.MCP_APPS_HOST_ENABLED` flips false → true. To remain opt-in, set `AGENTCORE_MCP_APPS_HOST_ENABLED=false` in inference-api task env. If MCP Apps is enabled but `mcp-sandbox` isn't deployed, `ui_resource` events emit with empty `sandboxOrigin` and the SPA cannot frame the App (#349)
- **App-api Bearer-only auth removed (#297).** External integrations calling `apis/app_api/` routes with `Authorization: Bearer` must switch to the API-key feature (`auth/api_keys/`, `X-API-Key`) before deploying beta.27. `POST /chat/agent-stream` remains Bearer-acceptable for non-SPA callers

### 🏗️ Infrastructure

- **New `ArtifactsStack`** (gated by `config.artifacts.enabled`) — DDB `user-artifacts` table, private S3 `artifacts-content` bucket, render Lambda, CloudFront on `artifacts.{domain}`, Route53 alias. Consumes `/artifacts/render-token-key-arn` SSM (published by `InfrastructureStack`); publishes `/artifacts/bucket-name`, `/artifacts/bucket-arn`, `/artifacts/table-name`, `/artifacts/table-arn`, `/artifacts/origin`. Requires `CDK_HOSTED_ZONE_DOMAIN`, `CDK_ARTIFACTS_CERTIFICATE_ARN` (must be in `us-east-1`)
- **New `McpSandboxStack`** (gated by `config.mcpSandbox.enabled`) — S3 mount-page bucket, CloudFront on `mcp-sandbox.{domain}` with a CloudFront Function for dynamic per-resource CSP, Route53 alias. Publishes `/mcp-sandbox/origin` SSM, consumed by inference-api at runtime as `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`. ACM cert must be in `us-east-1`
- **New `UserMenuLinksTable`** in `InfrastructureStack` + `/admin/user-menu-links-table-name` and `/admin/user-menu-links-table-arn` SSM parameters (#298)
- **New `ArtifactRenderTokenSecret`** in `InfrastructureStack` (Secrets Manager, AWS-managed encryption, `generateSecretString` 64-char) gated on `config.artifacts.enabled`. SSM `/artifacts/render-token-key-arn` publishes the ARN. Lives in `InfrastructureStack` (not `ArtifactsStack`) so app-api can read it without taking a stack-deploy-order dependency on `ArtifactsStack`
- **Inference-api conditionally consumes `mcp-sandbox` SSM** when `config.mcpSandbox.enabled` is true. Mirrors the artifacts conditional-SSM pattern; two synth tests cover present/absent (#349)

### 🔧 CI/CD

- **Backup workflow** wired as `workflow_dispatch` against the existing OIDC composite action (#361)
- **All five consumer workflows** now thread `CDK_HOSTED_ZONE_DOMAIN`, `CDK_ARTIFACTS_ENABLED`, `CDK_ARTIFACTS_CERTIFICATE_ARN` so synth-time validation doesn't fail on workflows that don't synth `ArtifactsStack` directly (#307)
- **Frontend build** runs `gen-version.js` explicitly before `ng build` so deployed bundles bake the real version (#336)
- **`infrastructure/test/infrastructure-stack.test.ts`** enumerates the 19 DDB tables instead of asserting `resourceCountIs(18)` (#350)
- **Docker `curl` pin** bumped to `8.14.1-2+deb13u3`; pin policy documented as "follow Debian point-releases" (#327)

### 📦 Dependency upgrades

- `bedrock-agentcore` 1.6.4 → 1.9.1 (with coupled `boto3` 1.42.96 → 1.43.9, `botocore` / `s3transfer` following). CHANGELOG audited end-to-end: no breaking changes for our memory/identity usage. Validated with a read-only dev smoke test (memory `get_memory_strategies` / `retrieve_memories` + identity `list_workload_identities`) and the full backend suite. Test-infra side effect: `botocore` 1.43 newly reads `Credentials.account_id` during endpoint construction; on a `RefreshableCredentials` (SSO) object that forces a refresh → `GetRoleCredentials`, which `moto` does not implement. Combined with `backend/src/.env`'s `AWS_PROFILE` leaking via `load_dotenv(override=True)`, this red-ed the suite order-dependently. Added per-test autouse scrub fixtures for `AWS_PROFILE` and the `DYNAMODB_*` / `COGNITO_*` config families, mirroring the existing `_clear_skip_auth_env` fixture for the same `.env`-bleed bug class (#337)
- `strands-agents` 1.39.0 → 1.40.0. Gated on a token-count audit and a compaction double-fire check. `use_native_token_count` default flipped true → false (Strands PR #2284) is inert for our token accounting — the flag gates only `BedrockModel.count_tokens()`, which Strands calls solely from `_estimate_input_tokens()` to populate `projected_input_tokens` on `BeforeModelCallEvent`. Our cost-badge / context-% / compaction-trigger plumbing reads from `inputTokens` + `cacheReadInputTokens` + `cacheWriteInputTokens` directly, so the flip is transparent (#340)

### 🧪 Test Coverage

- Backend + frontend regression coverage for `MaxTokensReachedException` classification, the `continue_truncated` resume path, `stream_error` always-allowed parser gating, and the `lastTurnContinuable` refresh-survival marker round-trip (#328)
- Backend regression coverage for adaptive thinking shape per model marker, `effort` allowed-set gating, and the float→int coercion path on `max_tokens` / `top_k` (#329, #330, #331)
- `infrastructure/test/mcp-sandbox-stack.test.ts` (264 lines) — synth + CFN unit coverage including the placeholder-substitution invariants (#343, #355)
- `infrastructure/test/mcp-sandbox-csp-function.test.ts` (357 lines) — `frame-ancestors` quote-escaping, including `'none'` (which would otherwise produce `''none''`, a JS syntax error) (#355)
- `infrastructure/test/inference-api-stack.test.ts` — two synth cases gating `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` wiring on `config.mcpSandbox.enabled` (#349)
- `infrastructure/test/cors.test.ts` (53 lines) — new CORS test surface
- `infrastructure/test/infrastructure-stack.test.ts` — 19 DDB tables enumerated with one-line justifications instead of count assertion (#350)
- Frontend specs: `mcp-app-bridge`, `mcp-app-card-state.service`, `mcp-app-consent.service`, `mcp-app-message.service`, `mcp-app-proxy.service`, `mcp-app-state.service`, `proxy-url`, `artifact-http.service`, `artifact-state.service`, `artifact-source.component`

### 📚 Docs

- `docs/kaizen/scoping/mcp-apps-host-renderer.md` — initial scoping document for the MCP Apps Host Renderer initiative (#296)
- `step-04-deploy.md` — "Register an MCP-Apps-capable MCP server" section with `budget-allocator-server` example + committed `ToolCreateRequest` payload (no auto-seed; registration stays an explicit per-env opt-in) (#349)
- `step-05-verify.md` — manual e2e dogfood scenario exercising all six Definition-of-Done MCP Apps interactions (#349)
- `docs/artifacts/...` — corrected cert-reuse guidance for subdomain primaries (#308)
- `CLAUDE.md` — `ui_resource` SSE row + deploy-order line updated for the live flag and conditional `mcp-sandbox` SSM consumption (#349)
- `.env.example` — documents `BFF_COOKIE_DATA_KEY_SECRET_ARN` (carry-over from beta.25) (#276)
- Architecture rules surfaced for Copilot CLI: 3-package import boundary, inference-api Runtime 404 trap, deploy order, SSE error model. Points to `.kiro/steering` and `.claude/skills` for deeper dives (#361)
- Forward-looking A2A guard: if exposing an A2A server, `AgentCard.capabilities` must include `streaming=True` or clients hang ~40 min (`sample-strands-agent-with-agentcore` commit `50c9112`) (#338)
- Kaizen-2026-05-15 hygiene — replaced dead source URLs in `kaizen-research` (the `bedrock/whats-new/` 404, the `docs.claude.com` claude-code release-notes 301→404, and the inactive `anthropics/courses`); fixed `aws/amazon-bedrock-agentcore-{sdk-python,starter-toolkit}` repo-slug typos to the correct `aws/bedrock-agentcore-*` slugs (#338, #341, #302, #304)

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
