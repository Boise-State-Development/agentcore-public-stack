# CLAUDE.md

Production-ready multi-agent conversational AI system built with AWS Bedrock AgentCore and Strands Agents.

## Build & Run

### Backend
```bash
cd backend
uv sync --extra agentcore --extra dev   # All deps
uv run python -m pytest tests/ -v       # Tests
cd src/apis/app_api && uv run python main.py    # App API (port 8000)
cd src/apis/inference_api && uv run python main.py  # Inference API (port 8001)
```

### Frontend
```bash
cd frontend/ai.client
npm install
npm run start    # Dev server (port 4200)
npm test         # Tests (Vitest via Analog.js)
```

### Infrastructure
```bash
cd infrastructure
npm ci
npx tsc --noEmit
npx jest
npx cdk synth
npx cdk deploy {prefix}-PlatformStack
```

## Key Conventions

- **Single CDK stack.** All AWS resources live in `PlatformStack`. There are no cross-stack dependencies to manage and no other CDK stack to deploy in a particular order. Application code is shipped out-of-band by `backend.yml` via AWS APIs (ECR push → ECS service update / Lambda code update / AgentCore Runtime update), not via CDK.
- **Deploy order:** `platform.yml` (CDK) when infra changes → `backend.yml` (parallel per-image: app-api, inference-api, rag-ingestion, artifact-render) → `frontend-deploy.yml` (S3 + CloudFront). Bootstrap data seeding is a one-time post-deploy step. Day-to-day backend code changes only run `backend.yml`.
- **Compute image URIs are read from SSM at CFN deploy time** (`/<prefix>/app-api/image-tag`, `/<prefix>/inference-api/image-tag`). The build pipeline owns those parameters; CFN always picks up the latest live image when re-registering the task def or Runtime, so infra-only deploys never revert a live service to the bootstrap stub.
- **Admin endpoints** go under `/admin/<domain>/`, user-facing under `/<domain>/`
- **Errors stream as assistant messages** via SSE (not HTTP error codes)
- **Signal-based state** throughout frontend (`signal()`, `computed()`)
- **Prompt-cache stability is a contract.** Bedrock prompt caching is exact-prefix-match: any list that reaches the system prompt or `toolConfig` (skills, tools, models, MCP tool listings) must be deterministically ordered at its source, and restored conversation history must be byte-stable between compaction-state changes (see the truncation anchor in `TurnBasedSessionManager`). An order flip or history mutation between turns silently re-writes a 30k–150k-token prefix at the cache-write premium, which is **1.25× the model's own base input rate** (2× at 1h TTL; a cache read is 0.1×) — there is no flat per-MTok figure, so price it against the model actually in play: $1.375/MTok on our default Haiku 4.5, $4.125 on Sonnet 4.6. Our `us.*` ids are **Regional (CRIS)** inference profiles and price ~10% above `global.*` for the same model; rates live in `curated-models.ts`. Per-model AWS **model cards** stay the primary source — they carry context windows, caching support, parameter tables and cutoffs, which no pricing API does — but the **Price List API now corroborates almost all of them**, and a rate worth trusting should agree in both. **Which offer file matters more than which vendor:** `--service-code AmazonBedrockFoundationModels` (372 us-west-2 SKUs) carries Claude, Cohere, Palmyra, TwelveLabs, Luma, Stability; `--service-code AmazonBedrock` (1052) carries Nova, xAI, Google, DeepSeek, Qwen and Moonshot. Querying the wrong one returns nothing and reads exactly like an unpublished model — which is how an earlier revision of this note concluded the API held "10 Claude SKUs, none newer than Claude 3." **That is no longer true** (re-verified 2026-09-21): every Claude model we curate is present and matches its card to the cent — Haiku 4.5 $1.10/$5.50 Regional and $1.00/$5.00 Global, Sonnet 4.6 $3.30/$16.50, Opus 4.7 $5.50/$27.50, Sonnet 5 $2.00/$10.00 Global, Fable 5.1 $10/$50 Global with a cache read of $0.25 that independently confirms its 0.025× multiplier. The 1.100× Regional premium reproduces throughout. **Two traps remain.** The `model` attribute is gone from the product schema — match on `servicename` (FoundationModels) or `usagetype` (AmazonBedrock), or every filter silently returns zero. And the hosted OpenAI family (`us.openai.gpt-5.6-*`, `us.openai.gpt-6-astra`, `openai.gpt-5.4`) is genuinely absent from **both** offer files, so its cards remain the only source; the old explanation that Marketplace billing is uncovered is wrong — Claude's rows are `MP:` usagetypes and are covered. `PROMPT_CACHE_OBSERVABILITY_ENABLED=false` disables the observability layer (fingerprint hook, cacheStatus derivation, EMF metrics) — the caching itself stays on
- **Token cost effectiveness is a design tenet — engineer against waste, not against context.** Before merging a change that touches the model call path, answer: what does this add to the prompt, on every turn, for the life of every session? (1) Anything in the cacheable prefix (system prompt, `toolConfig`, restored history) must be deterministic and append-only — see the prompt-cache contract above. (2) Per-turn payloads (tool results, MCP responses, retrieved documents) should be bounded or offloaded, never unbounded pass-through. (3) Don't guess — verify: `cacheStatus` + fingerprint hashes on the session's `C#` rows, `GET /admin/costs/sessions/{id}/calls`, and the `AgentCoreStack/PromptCache` EMF metrics exist to prove a change's cost impact. The balance: "cost effective" means eliminating waste (avoidable cache re-writes, duplicated context, oversized payloads) — it never means stripping context the model needs for a quality answer. When cost and answer quality genuinely conflict, quality wins; look for the cheaper path to the *same* quality, not a cheaper answer
- **Time to first token (TTFT) is a hard budget — add no latency before the first token.** That path is everything between a request arriving and the first streamed token: request handling in `inference_api/chat/routes.py`, agent build/cache lookup, MCP pre-flight, and every `BeforeInvocationEvent` / `BeforeModelCallEvent` hook — the latter fires before *every* model call, so its cost repeats on each tool round. Default pattern: a hook captures raw facts only, and anything derived from them is computed after the model answers, memoized (e.g. `get_context_breakdown(agent, itemized=True)` itemizes on read, not in `ContextAttributionHook`). If a change genuinely cannot avoid adding latency there, measure it at realistic scale (dozens of tools, a production-sized system prompt — not a toy fixture), state the number in the PR, and get the developer driving the change to accept it explicitly before merging. A millisecond or two sounds free; the preamble was cut from 455ms to 22–37ms, so each such addition is several percent of that budget, and they compound
- **One session can be served by more than one agent — never cache session state on an agent instance.** The agent cache keys on *configuration* (system prompt, tools, model, skills), so an `@`-mention turn builds a second `Agent`, and each `Agent` builds its own `TurnBasedSessionManager`. Both write the same DynamoDB session row and neither knows the other exists, while `initialize()` never re-runs on a cache hit — so anything a manager loads once and holds goes stale silently. This has bitten twice: conversation history (#741, fixed by aliasing the message list in `_adopt_session_conversation`) and compaction state (#751, fixed by re-reading it every turn in `_adopt_persisted_compaction_state`). Per-session state must be aliased across instances or re-read per turn, and must never move backwards — a clobbered checkpoint or truncation anchor is a prompt-cache **cost** bug before it is a correctness one
- **All dependencies use exact version pins** — no `^`, `~`, or `>=`
- **Never install packages without explicit user approval**

## Feature Flags

**New or in-development features default OFF. A deployment turns them on by choice.** Other organizations build on and deploy this stack, so half-finished work must not switch on for them just because it merged. When a feature is finished, flipping it to default-on (with an `=false` kill switch, like most shipped flags) is its own deliberate change.

A feature has up to two switches, one per side, set per environment:

| Side | Where | How it's set |
|---|---|---|
| Backend (authoritative) | `backend/src/apis/shared/feature_flags.py` (`<feature>_enabled()` reads `<FEATURE>_ENABLED`) | CDK `infrastructure/lib/config.ts` → env var on app-api / AgentCore Runtime, from the GitHub environment variable `CDK_<FEATURE>_ENABLED` forwarded in `.github/workflows/platform.yml`. In-development: only `"true"` enables. Unset GitHub variables arrive as `""`, so handle that case |
| Frontend (UI only) | `features` in `frontend/ai.client/src/environments/environment*.ts`, typed by `environments/feature-flags.ts`, read via the `FEATURES` token (`src/app/services/features.ts`) | Compile-time, per build: `environment.ts` = local `ng serve`/tests, `environment.development.ts` = deployed dev (`dev-deploy` configuration), `environment.production.ts` = prod. `scripts/frontend/build.sh` builds `SPA_BUILD_CONFIGURATION`, set by each deploy workflow |

- **The backend is the gate.** The frontend flag only decides what the UI offers (nav items, routes via `canMatch`, buttons), so nothing waits on a request to find out. Never import an environment file from a component; inject `FEATURES`, and provide it in specs.
- **Keep the two in step per environment.** If they drift, the damage is cosmetic: the UI offers a feature whose API 404s, or hides a working one.
- **Adding a flag:** add a field to `FeatureFlags` and all three environment files (in-development: `false` locally and in prod, `true` in dev only if the dev environment has turned the backend on), plus the backend flag, CDK config and test, and the `platform.yml` variable.

Current in-development flags: **Shared Projects** (`PROJECTS_ENABLED` / `CDK_PROJECTS_ENABLED`, `features.projects`). It's on in the `development` GitHub environment and in `environment.development.ts`, and off in production. **Compaction extract-then-compress** (`COMPACTION_SUMMARY_EXTRACT_ENABLED` / `CDK_COMPACTION_SUMMARY_EXTRACT_ENABLED`, runtime only, no SPA switch): on in both the `development` and `production` GitHub environments; still default-off for other deployers.

## SSE Event Types

| Event | Purpose |
|-------|---------|
| `message_start` | Start of assistant response |
| `content_block_start/delta/stop` | Streaming content |
| `message_stop` | End of message |
| `tool_use` / `tool_result` | Tool invocation and result |
| `ui_resource` | MCP App UI for a tool (SEP-1865) — payload `{type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin, serverName, icon, toolName}`. Emitted at the tool's `content_block_start` (early frame mount, so the App's bridge is live *while* the model streams the tool's arguments — see `ui_tool_input_partial`); falls back to right after the correlated `tool_result` if the name wasn't known at block start. At `content_block_start` a **header-only shell** (`html: ""`, full `serverName`/`icon`/`resourceUri` but no `resources/read`) is emitted *first* so the App frame's header (icon + server + tool + shimmer) replaces the plain tool rail with no flash; the full html-bearing event follows after the read and, last-write-wins, mounts the iframe (the SPA gates the iframe on a non-empty `html`). Deduped per `toolUseId` (the header shell on its own set, so it never blocks the full emit). HTML fetched server-side via `resources/read` and inlined; `sandboxOrigin` is the proxy.html origin the SPA frames it in (empty unless the mcp-sandbox stack is deployed — inference-api consumes its SSM origin only when `CDK_MCP_SANDBOX_ENABLED=true`; an empty origin means the SPA cannot frame the App). `serverName`/`icon`/`toolName` drive the App frame's connected header (Claude parity): `serverName`/`icon` resolve from the MCP `initialize` `serverInfo` (`title`→`name`, plus its `icons`); `serverName` falls back to the title-cased `ui://` authority, and `icon` falls back to the server's served MCPB `manifest.json` icon — fetched server-side from `<server-origin>/manifest.json` (same-origin only, cached per origin, base64-inlined as a `data:` URI; the runtime MCP protocol carries no icon, so this mirrors what Claude inlines from the installed bundle), else empty (→ generic glyph). A large auto-fetched icon is NOT persisted (size-gated against the 400KB DynamoDB item limit), so it shows live but reloads to the glyph; `toolName` is the agent-facing tool name, carried on the event so the header's name + running shimmer appear atomically with the frame's promotion (not gated on the separately-streamed message content). All three persist with the resource so the header survives reload. Gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7; set `=false` to opt an environment out) |
| `ui_tool_input_partial` | Streamed partial tool input for a UI tool (SEP-1865 `ui/notifications/tool-input-partial`) — payload `{type, toolUseId, arguments}`. Emitted repeatedly while the model is still streaming a UI tool's arguments (after the early `ui_resource` mount); `arguments` is the streamed prefix server-side "healed" into a valid object (`apis/shared/mcp_apps/partial_json.py`). The SPA relays each to the App via `ui/notifications/tool-input-partial` so a progressively-rendering App (e.g. Excalidraw's guided camera tour) animates as args arrive; the complete `tool-input` follows once the input is final. Same gating as `ui_resource` |
| `agent_status` | What the agent is doing right now — payload `{type, sessionId, phase, cycle, toolName?, toolUseId?, durationMs?, ok?}`. Emitted from `AgentStatusHook` (`BeforeModelCall` / `Before`+`AfterToolCall`) and drained in `stream_coordinator` exactly like `steering_applied`, before the event it precedes, so "Using list_assignments" reaches the client while that tool is running rather than after its result. Phases: `thinking` (one per event-loop cycle — a three-tool turn reports it four times, and `cycle` distinguishes them), `tool_start`, `tool_end` (carries Strands' own measured `durationMs`, and `ok=false` for BOTH a raised exception and a result with `status: "error"`). There is deliberately **no "responding" phase** — the SPA already knows text is streaming from the deltas, and a backend-derived duplicate of a fact the client holds first-hand would only disagree at the edges. Durations are live-only and deliberately NOT persisted: a reloaded conversation shows summaries without timings, where a client-invented number would be one the user could not trust. Costs nothing against the model — nothing it produces reaches the prompt, so the cacheable prefix is untouched. Gated by `AGENT_STATUS_ENABLED` (default on with a kill switch); while off the hook is registered but every callback returns immediately and the SPA stays on its cycling phrases |
| `tool_group_summary` | Model-generated one-line summary of a finished tool batch — payload `{type, sessionId, batchId, toolUseIds, summary}`, e.g. "Found the Syllabus Acknowledgment assignment in BIO 101". Produced by a Nova Micro **side-channel** task (`apis/shared/tool_summaries/summarizer.py`) structured exactly like `session_title`: its own Bedrock call on its own messages, concurrent with the agent stream, so it **never appends to the conversation** and adds nothing to the cacheable prefix. Spend is one bounded call per batch (inputs/results truncated at capture in the hook AND again in the summarizer). Lands mid-turn, out of band with the content stream, so it can arrive after the rail that shows it has rendered; the SPA keys it by `toolUseId` (not batch — client-side grouping need not match backend batches, and a group spanning two batches shows the first batch's line). Persisted as `TSUM#` rows in sessions-metadata, reusing the `SessionLookupIndex` GSI — zero new infra — and replayed on `GET /messages` as `toolSummaries`, because the event never re-streams. Deliberately NOT written onto the message content blocks: that is the Converse payload and the cacheable prefix, so a display string there would be paid at model rates every subsequent turn. Gated by `TOOL_SUMMARIES_ENABLED` (default on with a kill switch); while off the SPA's deterministic client-side formatter still renders ("Listed 4 assignments"), so absence is a downgrade in specificity, never a blank |
| `session_title` | Server-generated conversation title on a session's FIRST turn — payload `{type, sessionId, title}`. Title generation (Nova Micro) runs as an asyncio task concurrent with the agent stream; the finished title is interleaved between agent events (non-blocking done-check in `stream_with_quota_warning`), so the sidebar/top-nav rename while the response is still pending. Emitted at most once per stream, possibly after `done` (the SPA parser allowlists it past Completed-state gating); never carries the "New Conversation" placeholder. Best-effort: a stream that finishes before generation emits nothing — the SPA's post-close metadata refresh (`refreshTitleFromServer`) is the fallback, reading the title the task also persisted via `update_session_title` |
| `quota_session_notice` | **This conversation** has reached the tier's session-notice share of the monthly limit — payload `{type, sessionId, sessionCost, quotaLimit, sessionPercentageOfLimit, thresholdPercentage, message}`. Emitted at the head of the stream right after `quota_warning`, and re-emitted every turn while over the share (dismissal is client-side, same contract as `quota_warning`). `sessionCost` is the session's **lifetime** cost — the `totalCost` aggregate on its metadata row — deliberately not period-scoped: a conversation that opened last month and is spending this month's budget is exactly the one worth surfacing. Share is tier-configurable (`sessionNoticePercentage`, default 25%, 0 disables); the whole runway rides the `QUOTA_RUNWAY_ENABLED` kill switch (default on), which also gates the 50%/75% `quota_warning` rungs. The SPA scopes it to the conversation it names — never shown above another thread's composer |
| `agent_notice` | A Shared Project's harness is running this turn **without** some of its setup — payload `{type, sessionId, agentId, projectId?, message, unavailableModelId?, unavailableTools, unavailableSkills, unavailableMemory?}`. Only a project harness degrades (shared-projects §9.6): a member missing a bound tool, skill, model or memory space still gets the turn, with that piece dropped — tools keyed by *server* so no later scoped ref of a denied server slips through, an unavailable model falling through to the member's default, and an all-dropped toolset still meaning "no tools", never the request's. Every other shared agent keeps block-with-message (D5, `stream_error`). Emitted before `message_start`, beside `quota_session_notice`. **SSE only, never the prompt** — the model is not told what was dropped, so the cacheable prefix is unaffected — and not persisted, because the next turn re-derives it. The SPA ignores it until the Projects UI (PR-1.8) renders it |
| `model_retry` | Backend is retrying a failed model call instead of surfacing it — payload `{type, attempt, delaySeconds}`. Emitted from Strands' `EventLoopThrottleEvent`; `attempt` is 1-based and counted per turn in `stream_processor` (the raw event carries only the delay). **Timing caveat:** Strands sleeps the backoff *inside* its hook and yields the event afterwards, so it lands as the next attempt BEGINS, not when the wait starts — `delaySeconds` describes the gap just endured, it is not a countdown. It also cannot cover the failing model call itself, which is indistinguishable from a slow healthy one. The SPA swaps the loading indicator's cycling phrases for a fixed amber notice, cleared on `message_start`/`done` |
| `steering_applied` | A follow-up the user typed **while the turn was still streaming** was injected into that running turn at a tool boundary — payload `{type, sessionId, entryId, text}`. PR #916 queues a mid-stream Enter in the composer and flushes it on the turn's falling edge; mid-turn steering lands it at the next tool boundary instead, appended as a `{"text": ...}` block on the same user-role message that carries the tool results, so the agent reads it before choosing its next action. Emitted after that batch's `tool_result` events (so the thread renders in the order the model will see) and never after `done`. The transport in is the session's single-flight **lease row** — `steerQueue` + `steerFor`, owner-scoped exactly like `cancelRequestedFor`, armed by app-api's `POST /sessions/{id}/steer` and deleted with the lease at turn end. Consumption is **commit-on-append**: the hook peeks at `AfterToolsEvent` and clears the inbox entry only on the `MessageAddedEvent` for that same message, because `AfterToolsEvent` fires from a `finally` and so also fires on the interrupt path, where the mutated message is discarded — a hook that consumed on read would destroy the user's words whenever a steer landed on the same tool batch as an OAuth consent. Absence of this event is the fallback, not an error: a turn that calls no tools has no boundary to inject at, and a steer can lose the race with the turn's end — in both cases the entry stays queued and #916's end-of-turn flush sends it as a normal turn. Append-only against the cached prefix (the injection lands inside the segment the `strategy="auto"` message-level cachePoint covers), so it never rewrites the prefix. Gated by `MID_TURN_STEERING_ENABLED` (default on with a kill switch); while off the steer endpoint 404s and the hook returns immediately. See `docs/specs/mid-turn-steering.md` |
| `stream_error` | Conversational error |
| `oauth_required` | External MCP tool needs user consent — payload `{providerId, authorizationUrl, interruptId?}`, one event per provider emitted after `message_stop`. Two flavors. **Interrupt-driven** (`interruptId` present): `OAuthConsentHook` paused a tool call mid-turn; the SPA resumes that exact turn by POSTing the id back. **Pre-flight** (`interruptId` *absent*): the tool never registered because the MCP server refused the pre-flight `tools/list` — since the consent hook is `BeforeToolCall`, it can't fire for an unregistered tool, so without this event the tool vanishes with no explanation. Nothing is paused, so the SPA shows the Connect affordance and must NOT resume; a synthetic id would be worse than none, because the resume guard in `inference_api/chat/routes.py` 400s on unknown ids and the user would hit an error right after consenting. Pre-flight events are re-emitted each turn that rebuilds the agent (the pre-flight keeps failing until consent lands) and are deliberately not persisted as `pending_interrupt` breadcrumbs — those are keyed by interrupt id for the resume path. The SPA dedupes by `providerId` and suppresses a dismissed pre-flight prompt for the tab session. Before emitting, the runtime asks the AgentCore vault directly: a vaulted token is warmed into `oauth_token_cache` and the pre-flight retried, so a user who already consented gets the tool back instead of losing it for the life of the process |
| `user_question_required` | The agent paused to ask the user structured clarifying questions — payload `{type, interruptId, toolUseId, questions}`, where each question is `{header, question, multiSelect, options: [{label, description?}]}`. Emitted after `message_stop` in the same `done` block as `oauth_required` / `tool_approval_required`, one event per pending prompt. Unlike those two the interrupt is raised by the **tool itself** (`ask_user_question`, via `ToolContext`) rather than by a `BeforeToolCall` hook; Strands routes both through `_stop_for_interrupts`, so the `PausedTurnSnapshot`, the resume route and the `PendingInterrupt` breadcrumb (`kind: "user_question"`, questions JSON-encoded) need no special case. The SPA renders a picker and resumes the same turn by POSTing an `interrupt_responses` entry whose `response` is `{answers: {"<header>": {selected, text}}}` — always an object, **never null**: `ToolContext.interrupt` only treats a non-None response as an answer, so a null would re-raise the interrupt forever ("Skip" sends `{skipped: true}`). The picker always offers Other + Skip, so the model must not include them as options. Cost: the tool spec is a constant in the cacheable `toolConfig` prefix and the questions travel on the SSE channel only — just the one-line formatted answer block re-enters the conversation. Gated by `ASK_USER_QUESTION_ENABLED` (default on with a kill switch); while off the tool is never registered, so the model falls back to asking in prose |
| `browser_login_required` | The agent paused so the **user** can sign in to a site it cannot reach — payload `{type, interruptId, toolUseId, sessionId, browserSessionId, browserId, viewport, deadlineAt?, targetUrl?, reason?}`. Emitted after `message_stop` in the same `done` block as `oauth_required` / `user_question_required`, one per pending handover. Raised by the tool itself (`request_user_login`, via `ToolContext`) like `ask_user_question`, so the `PausedTurnSnapshot`, the resume route and the `PendingInterrupt` breadcrumb (`kind: "browser_login"`) need no special case. `sessionId` is the **conversation** id as everywhere else; the browser is `browserSessionId` — app-api's live-view route is owner-scoped by conversation and resolves the browser server-side. **There is deliberately no URL on this event.** `generate_live_view_url` signs with SigV4 *query* auth and caps at 300s, so a minted URL is dead before a human reacts and dead again on reload; app-api mints one per request instead (`assert_no_url` enforces this on the interrupt, the event and the persisted row, rather than trusting it). `viewport` rides the event because DCV's `remoteWidth`/`remoteHeight` must match the session viewport or the stream crops — a second copy of `1280x800` in the SPA is a copy that will drift. Resume posts `{completed: true}` / `{skipped: true}` — always an object, **never null**, or the interrupt re-raises forever. While the user holds the browser the automation stream is `DISABLED` at the service, so the agent provably cannot act; the idle reaper exempts the session until `deadlineAt` and no further, which is what stops a walked-away-from takeover billing to its TTL. Cost: the tool spec is a constant in `toolConfig` for granted users (~200 tokens) and the whole round trip adds one short line to the conversation — the live view is a stream the model never sees. Gated by `BROWSER_TAKEOVER_ENABLED` (default on with a kill switch) **and** by RBAC: `request_user_login` is its own catalog entry (`enabledByDefault: false`), because RBAC granularity is one `tool_id` and an action on `browse_web` would have shipped an interactive browser in our AWS account to every user who can browse. See `docs/specs/authenticated-web-assessment.md` |
| `compaction` | Backend rolled older turns into a summary on this turn — payload `{previousCheckpoint, newCheckpoint, summarizedTurns, inputTokens}`, emitted after the final `metadata` event so the badge updates first, before `done` |
| `done` | Stream complete |

## Multi-Protocol Tool Architecture

| Protocol | Location | Auth |
|----------|----------|------|
| Direct call | `agents/main_agent/tools/` | N/A |
| AWS SDK | `agents/main_agent/tools/` | IAM |
| MCP + SigV4 | Cloud Lambda (Gateway) | AWS SigV4 |
| A2A | Cloud Runtime | AgentCore auth |

Today A2A is **client-only** — `A2AAgentConfig` (`apis/shared/tools/models.py`) describes remote agents we call out to; we do not yet expose an A2A server / `AgentCard`. **When the first A2A server construct lands** (Strands `agent.to_a2a()`, an `A2AServer`, or a hand-built `AgentCard`), its advertised `capabilities` MUST include `streaming=True`. Without it the A2A SDK client silently falls back to non-streaming, never receives a `completed` event, and hangs until its ~40-minute timeout (ref-repo `aws-samples/sample-strands-agent-with-agentcore` commit `50c9112`).

## Cross-Package Contracts

- Backend route handlers define the API shape; frontend TypeScript interfaces must match
- Breaking changes to an endpoint require updating both packages in the same PR
- SSE event types are part of the contract — changes must be coordinated
- `app_api`, `inference_api`, and `agents/` are independent consumers of `apis.shared`. They must never import from each other — only from `apis.shared`. If something is needed by more than one of them, move it to `apis.shared`. This is enforced by `tests/architecture/test_import_boundaries.py`.

## File Creation Rules

| Change Type | Where It Goes |
|-------------|---------------|
| New API route | `backend/src/apis/app_api/<domain>/` |
| New admin endpoint | `backend/src/apis/app_api/admin/<domain>/` |
| New agent tool | `backend/src/agents/main_agent/tools/` + register in `__init__.py` |
| New Angular page | `frontend/ai.client/src/app/<feature>/` |
| New CDK construct | `infrastructure/lib/constructs/<area>/` (compose into `PlatformStack` via `lib/platform-stack.ts`; if it needs to expose values to compute constructs, thread them through `PlatformComputeRefs` rather than via SSM) |
| Note: there are no separate CDK *stacks* anymore — every resource lives in the single `PlatformStack`. |
| New Lambda for an infra construct | `backend/src/lambdas/<lambda-name>/` (one folder per Lambda; not part of the `apis/` import boundary) |
| Shared backend code | `backend/src/apis/shared/<domain>/` |

### Inference API boundary

The `inference-api` runs inside an AgentCore Runtime container. The runtime data plane (`https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/...`) only proxies the standard AgentCore paths — `POST /invocations` and `GET /ping`. **Other HTTP paths return 404 from AWS before the request ever reaches the container**, so any custom route added to inference-api is unreachable in cloud. Local dev is misleading because `localhost:8001` bypasses the runtime gateway entirely.

**Rule:** Don't add routes to `inference-api` unless they're part of the AgentCore Runtime invocation path (i.e. the chat router that owns `/invocations` and `/ping`). User-facing CRUD-ish endpoints — even ones that share dependencies with the agent loop — belong in `app-api`. If a coding agent suggests adding endpoints to inference-api "because it has the workload context," push back: the right pattern is to mint workload context on app-api via the `AGENTCORE_RUNTIME_WORKLOAD_NAME` mint fallback in `apis/shared/oauth/agentcore_identity.py`.

If you're tempted to add to inference-api because of an existing route there (`converse_router`, `voice_router`), don't use them as templates without confirming their access path — they predate this rule and may rely on bypasses (API key, WebSocket upgrade, direct container reach in environments where the runtime isn't the only path).

### RBAC: the AppRole record is the source of truth

A role grants a tool/model/skill by listing it in its own `grantedTools` / `grantedModels` / `grantedSkills` (or by granting the `*` wildcard, or inheriting from a parent). **That is the only thing access checks read.** The `allowedAppRoles` field on a resource (tool, model, skill) is a *derived, display-only* projection of those role records — never an independent grant.

**Rule:** When an admin page offers a "which roles can use this?" picker on the resource, it must write *through* to each role's `granted*` list (`set_roles_for_tool`, `set_roles_for_model`) and derive the field back on read (`hydrate_model_roles`). Never persist a role list on the resource and expect it to grant anything: it will silently do nothing, and the resource page and the role page will disagree.

Access decisions for models live in `apis/app_api/admin/services/model_access.py`. `can_access_model` and `filter_accessible_models` both delegate to a single `_grants_access` predicate — keep it that way. They previously diverged (one gated on `allowed_app_roles`, one didn't), so a model could be *listed* by the catalog and *denied* on use.

### Auth dependency on app_api routes

The SPA sends an httpOnly session cookie — not `Authorization: Bearer`. A route that declares a bare Bearer-only dependency on the SPA-facing surface causes a 401 → centralized redirect loop the moment the SPA hits it.

**Rule:** New routes under `apis/app_api/` use `Depends(get_current_user_from_session)` from `apis.shared.auth.dependencies` for user authentication. Admin routes use `Depends(require_admin)` (which chains through the same cookie dependency). The only exceptions are the API-key feature (`auth/api_keys/`, uses `X-API-Key`) and voice mode (`voice/`, uses a voice-ticket cookie) — both handle auth on their own terms and should not be used as templates for ordinary user routes.

Do not reintroduce a Bearer-only `Depends(...)` on any user-facing route. If you find one in older code, migrate it to `get_current_user_from_session`.

### App shell chrome: `data.chrome` on the route

The app shell (`app.ts` / `app.html`) decides what surrounds a page by reading `data.chrome` off the active route, never by testing the URL. Three states:

| `chrome` | Sidenav | Content box |
|----------|---------|-------------|
| *(unset)* | chat nav | `mx-auto max-w-7xl` |
| `'minimal'` | none | none — page fills the shell (the artifact viewers, `artifacts/:id` and `shared-artifact/:shareId`) |
| `'admin'` | **admin nav** (`AdminNav` replaces the sidenav's body) | none — `AdminLayout` owns its own padding |

**The walk is deepest-*declared*-wins, not leaf-only** (`shared/utils/route-chrome.ts`). `'admin'` is declared once on the parent `/admin` route and every child declares nothing; with the default `paramsInheritanceStrategy` (`'emptyOnly'`) Angular does **not** copy `data` onto a child that has its own path and component, which every admin page has. A leaf-only read therefore sees nothing on any of them. A leaf can still opt out by declaring something else (`'full'`).

**Rule:** The admin console's navigation is the *sidenav's body*, not a column inside the page. Never re-add an `<aside>` nav to an admin page — the point of the swap is that admin surfaces (cost tables, the model catalog, the role matrix) get the full content width instead of sitting between two navigations, one of which lists conversations that cannot be opened from `/admin`.

Three things that look optional and are not:

- **`Sidenav.isAdminChrome` and `App.adminChrome` both read the flag.** Do not substitute a `/admin` URL test in either: two independent answers to "are we in the console?" is one more than can stay in agreement.
- **`AdminLayout` carries its own small-screen nav opener.** The shell's floating hamburger is gated on `HeaderService.showContent()`, state the *session page* owns and admin pages never set — relying on it would make the admin nav reachable or not depending on where the user had been first.
- **`.sidenav-body-enter` is global in `styles.css` on purpose.** The chat nav and the admin nav swap into the same slot and must arrive identically. Angular scopes keyframe *names* per component, so a copy of the rule inside a component compiles to a separately-named animation that looks identical today and drifts the first time one is tuned. Add a section to the stagger by applying the class plus an `animation-delay`, not by writing new keyframes.

## Debugging Quick Reference

- **Tool not appearing:** Check `__init__.py` export, RBAC permissions, `enabled_tools`, ToolRegistry
- **Session not persisting:** Check AgentCore Memory config, session_id, TurnBasedSessionManager flush
- **SSE stream disconnecting:** Check 600s timeout, client connection, quota exceeded events
- **Usage at $0 / not counted against quota:** the call's model id has no catalog row, so it priced to `None`. The `UnmeteredModelCall` EMF metric (PromptCache namespace, alarm threshold 0) fires per such call; the dashboard's "Unmetered calls by model" widget names the id. A turn that names no model runs on the user's saved default, then the catalog's `isDefault` row (`_resolve_fallback_model`); the hard-coded `Defaults.MODEL_ID` is reached only when the catalog has no enabled default, and prod has no row for it
- **Cost spike / cache miss:** Check `cacheStatus` + `toolConfigHash`/`systemPromptHash`/`historyHash` on the session's `C#` rows in sessions-metadata (or `GET /admin/costs/sessions/{id}/calls`) — the hash that changed between consecutive calls names the cache-buster. Rule out the explained case first: `agentSwitched` on the row means an `@`-mention ran that turn on a different Agent, which re-writes the prefix on purpose and looks identical to the regression (`agentSwitchMissCount`/`agentSwitchUsd` are a subset of the totals — subtract for unexplained waste). **A `hit` is not proof the prefix was cached** — `partial_miss` marks the calls that read a leading segment (tools+system) while re-writing the history behind it, which costs like a full miss; its tell is a flat `cacheReadInputTokens` next to a `cacheWriteInputTokens` that tracks the whole conversation, and its dollars are in `wastedUsd` with `partialMissCount`/`partialMissUsd` naming the subset. Rows written before that status shipped still say `hit` — nothing is backfilled

## Coding Standards

### Python
- `snake_case` functions/variables, `PascalCase` classes
- Type hints required on all function signatures
- No `print()` in production — use `logging`

### TypeScript
- `camelCase` functions/variables, `PascalCase` classes/interfaces, `UPPER_SNAKE_CASE` constants
- Strict mode enabled — no `any` unless absolutely necessary

### General
- No commented-out code in commits
- Prefer early returns over deep nesting
- One logical change per commit using conventional commits (`feat:`, `fix:`, `chore:`, etc.)

## Git Workflow

- Branch from `develop`, never `main`
- PRs target `develop`; `main` is updated via squash-merge releases only
- Branch naming: `feature/<short-description>`
