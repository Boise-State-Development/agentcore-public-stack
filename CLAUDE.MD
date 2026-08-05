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
- **Prompt-cache stability is a contract.** Bedrock prompt caching is exact-prefix-match: any list that reaches the system prompt or `toolConfig` (skills, tools, models, MCP tool listings) must be deterministically ordered at its source, and restored conversation history must be byte-stable between compaction-state changes (see the truncation anchor in `TurnBasedSessionManager`). An order flip or history mutation between turns silently re-writes a 30k–150k-token prefix at the $2.50/MTok cache-write premium. `PROMPT_CACHE_OBSERVABILITY_ENABLED=false` disables the observability layer (fingerprint hook, cacheStatus derivation, EMF metrics) — the caching itself stays on
- **Token cost effectiveness is a design tenet — engineer against waste, not against context.** Before merging a change that touches the model call path, answer: what does this add to the prompt, on every turn, for the life of every session? (1) Anything in the cacheable prefix (system prompt, `toolConfig`, restored history) must be deterministic and append-only — see the prompt-cache contract above. (2) Per-turn payloads (tool results, MCP responses, retrieved documents) should be bounded or offloaded, never unbounded pass-through. (3) Don't guess — verify: `cacheStatus` + fingerprint hashes on the session's `C#` rows, `GET /admin/costs/sessions/{id}/calls`, and the `AgentCoreStack/PromptCache` EMF metrics exist to prove a change's cost impact. The balance: "cost effective" means eliminating waste (avoidable cache re-writes, duplicated context, oversized payloads) — it never means stripping context the model needs for a quality answer. When cost and answer quality genuinely conflict, quality wins; look for the cheaper path to the *same* quality, not a cheaper answer
- **One session can be served by more than one agent — never cache session state on an agent instance.** The agent cache keys on *configuration* (system prompt, tools, model, skills), so an `@`-mention turn builds a second `Agent`, and each `Agent` builds its own `TurnBasedSessionManager`. Both write the same DynamoDB session row and neither knows the other exists, while `initialize()` never re-runs on a cache hit — so anything a manager loads once and holds goes stale silently. This has bitten twice: conversation history (#741, fixed by aliasing the message list in `_adopt_session_conversation`) and compaction state (#751, fixed by re-reading it every turn in `_adopt_persisted_compaction_state`). Per-session state must be aliased across instances or re-read per turn, and must never move backwards — a clobbered checkpoint or truncation anchor is a prompt-cache **cost** bug before it is a correctness one
- **All dependencies use exact version pins** — no `^`, `~`, or `>=`
- **Never install packages without explicit user approval**

## SSE Event Types

| Event | Purpose |
|-------|---------|
| `message_start` | Start of assistant response |
| `content_block_start/delta/stop` | Streaming content |
| `message_stop` | End of message |
| `tool_use` / `tool_result` | Tool invocation and result |
| `ui_resource` | MCP App UI for a tool (SEP-1865) — payload `{type, toolUseId, resourceUri, html, mimeType, csp, permissions, sandboxOrigin, serverName, icon, toolName}`. Emitted at the tool's `content_block_start` (early frame mount, so the App's bridge is live *while* the model streams the tool's arguments — see `ui_tool_input_partial`); falls back to right after the correlated `tool_result` if the name wasn't known at block start. At `content_block_start` a **header-only shell** (`html: ""`, full `serverName`/`icon`/`resourceUri` but no `resources/read`) is emitted *first* so the App frame's header (icon + server + tool + shimmer) replaces the plain tool rail with no flash; the full html-bearing event follows after the read and, last-write-wins, mounts the iframe (the SPA gates the iframe on a non-empty `html`). Deduped per `toolUseId` (the header shell on its own set, so it never blocks the full emit). HTML fetched server-side via `resources/read` and inlined; `sandboxOrigin` is the proxy.html origin the SPA frames it in (empty unless the mcp-sandbox stack is deployed — inference-api consumes its SSM origin only when `CDK_MCP_SANDBOX_ENABLED=true`; an empty origin means the SPA cannot frame the App). `serverName`/`icon`/`toolName` drive the App frame's connected header (Claude parity): `serverName`/`icon` resolve from the MCP `initialize` `serverInfo` (`title`→`name`, plus its `icons`); `serverName` falls back to the title-cased `ui://` authority, and `icon` falls back to the server's served MCPB `manifest.json` icon — fetched server-side from `<server-origin>/manifest.json` (same-origin only, cached per origin, base64-inlined as a `data:` URI; the runtime MCP protocol carries no icon, so this mirrors what Claude inlines from the installed bundle), else empty (→ generic glyph). A large auto-fetched icon is NOT persisted (size-gated against the 400KB DynamoDB item limit), so it shows live but reloads to the glyph; `toolName` is the agent-facing tool name, carried on the event so the header's name + running shimmer appear atomically with the frame's promotion (not gated on the separately-streamed message content). All three persist with the resource so the header survives reload. Gated by `AGENTCORE_MCP_APPS_HOST_ENABLED` (default true since PR #7; set `=false` to opt an environment out) |
| `ui_tool_input_partial` | Streamed partial tool input for a UI tool (SEP-1865 `ui/notifications/tool-input-partial`) — payload `{type, toolUseId, arguments}`. Emitted repeatedly while the model is still streaming a UI tool's arguments (after the early `ui_resource` mount); `arguments` is the streamed prefix server-side "healed" into a valid object (`apis/shared/mcp_apps/partial_json.py`). The SPA relays each to the App via `ui/notifications/tool-input-partial` so a progressively-rendering App (e.g. Excalidraw's guided camera tour) animates as args arrive; the complete `tool-input` follows once the input is final. Same gating as `ui_resource` |
| `session_title` | Server-generated conversation title on a session's FIRST turn — payload `{type, sessionId, title}`. Title generation (Nova Micro) runs as an asyncio task concurrent with the agent stream; the finished title is interleaved between agent events (non-blocking done-check in `stream_with_quota_warning`), so the sidebar/top-nav rename while the response is still pending. Emitted at most once per stream, possibly after `done` (the SPA parser allowlists it past Completed-state gating); never carries the "New Conversation" placeholder. Best-effort: a stream that finishes before generation emits nothing — the SPA's post-close metadata refresh (`refreshTitleFromServer`) is the fallback, reading the title the task also persisted via `update_session_title` |
| `stream_error` | Conversational error |
| `oauth_required` | External MCP tool needs user consent — payload `{providerId, authorizationUrl}`, one event per provider emitted after `message_stop` |
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

## Debugging Quick Reference

- **Tool not appearing:** Check `__init__.py` export, RBAC permissions, `enabled_tools`, ToolRegistry
- **Session not persisting:** Check AgentCore Memory config, session_id, TurnBasedSessionManager flush
- **SSE stream disconnecting:** Check 600s timeout, client connection, quota exceeded events
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
