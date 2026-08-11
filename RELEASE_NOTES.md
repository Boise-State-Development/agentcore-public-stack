# Release Notes — v1.14.0

**Release Date:** August 11, 2026
**Previous Release:** v1.13.0 (August 2, 2026)

---

> 🏗️ **CDK deploy required.** Run `platform.yml` first, then `backend.yml`, then `frontend-deploy.yml`. `infrastructure/lib/` changed in three places this release (runtime log-group wiring, one environment variable removed, new prompt-cache widgets and alarm). **No data migration, no GSI changes** — `infrastructure/gsi-inventory.json` is byte-identical to `main`.

---

## Highlights

The platform gets an interface that isn't a browser. **`agentcore-tui`** is a terminal chat client — streaming, model picker, command palette, keyring-backed auth — shipped as its own `uv` project so it installs with `uvx agentcore-tui` without touching this repo.

The rest of the release is the cost-effectiveness arc turning into shipped code. **Conversations now pin to a microVM**, which roughly halves steady-state turn latency for *every* session: AgentCore routes by runtime session id, nothing was forwarding one, and consecutive turns of a single conversation kept landing on cold containers. **Prompt-cache observability learns to name `partial_miss`** — the classification that had been reporting 90% of a burned monthly quota as a green `hit`. And **quota warnings gain a runway**: earlier rungs, plus a per-conversation cost notice for the case the old ladder structurally could not catch, where one conversation spends most of a user's month.

One correctness fix is worth reading even if you skip the rest. **`isPublic` on a tool granted nothing.** It was read by the admin picker and by nothing that enforces access, so three document-builder tools listed for every user in production and then refused at use — including on public marketplace agents that bind them.

---

## A terminal client

The platform's first non-browser interface. `tui/` is a standalone `uv` project — not part of the backend's dependency graph — built on Textual 8.2.8 and talking to the API-key authenticated `/chat/api-converse` endpoint. It is distributable on its own: `uvx agentcore-tui`.

This is Phase 1. It chats, streams, switches models, and reports usage.

### Client
- `client/converse.py` / `client/events.py` — streaming SSE with typed events, and typed **actionable** errors mapped from the endpoint's HTTP contract: 401, 403, 429, 400 and 502, plus mid-stream failures that arrive as events rather than status codes
- `config.py` — resolution across CLI flags, environment, a TOML file and the OS keyring, degrading gracefully on hosts with no keyring backend rather than refusing to start
- `logging_setup.py` — rotating file logs with prompt content **redacted** unless `AGENTCORE_LOG_CONTENT=1`; the API key is never logged at any level

### Interface
- `app.py` / `widgets/` — live Markdown rendering with delta coalescing, a collapsible reasoning pane, token and usage status bar, and cancel-in-flight
- `screens/model_picker.py` — model picker on F2; curated command palette on F1 (new conversation, change model, copy last response or transcript, theme, log path)
- `cli.py` — `chat`, `login` (getpass + keyring), `logout`, `status` (health probe)

### Local development
`scripts/local-dev/` runs the client against a live environment: `start-app-api.sh` (loopback-bound, and it refuses a non-loopback bind while the auth bypass is on), `mint-api-key.sh`, `sync-models.sh`, `run-tui.sh`, and a host-side `tui.sh` launcher.

### Version discipline
`tui/pyproject.toml`, `tui/src/agentcore_tui/__init__.py` and `tui/uv.lock` are wired into `scripts/common/sync-version.sh`, so the new project's manifests cannot drift from `VERSION` — the same gate the other three packages sit behind.

### Test Coverage
1,400+ lines across 143 tests, none of which need the network: `httpx.MockTransport` for the client and Textual's `run_test` pilot for the interface. Also exercised end to end against a real app-api, over both the Bedrock and Mantle model paths, plus the 401 and connection-failure surfaces.

---

## Conversations stay on one microVM

Turns got about twice as fast, and the reason is embarrassingly simple: nothing was telling AgentCore that two turns belonged to the same conversation.

AgentCore routes an invocation to a microVM by runtime session id. We never forwarded one, so AWS assigned a fresh session per call — and consecutive turns of a single conversation could land on different containers, where inference-api's in-process agent cache is cold by definition.

Measured in dev with a two-arm A/B differing only by this header, at steady state:

| | turn latency |
|---|---|
| No pinning, agent-cache miss | ~7.6s |
| Pinned, agent-cache miss | ~4.8s |
| Pinned, agent-cache hit | ~3.9s |

Read the split carefully, because an earlier note in this codebase got it wrong and has been corrected: **most of the win is the warm container**, which every session gets. The agent-cache hit adds roughly 19% on top, and only for the subset of sessions that can use it. Both arms produced an identical prompt-cache token split (write:read 0.336) — **this is a latency fix, not a cost one.** An earlier probe suggested a cost win; that was run-order confound, where the second arm inherited the first's Bedrock cache entry because both primed with byte-identical text.

### Backend
- `apis/shared/harness/runner.py`, `apis/app_api/chat/proxy_routes.py`, `apis/app_api/mcp_apps/routes.py` — forward a runtime session id derived as a sha256 of our session id. The hash is not decoration: the runtime session id has a charset and a 33-character minimum our session ids don't reliably meet, and a hash is always valid, stable per conversation, and keeps our identifiers out of an AWS-side one
- Kill switch `AGENTCORE_RUNTIME_SESSION_AFFINITY_ENABLED=false` restores per-call runtime sessions exactly

**This was also the missing prerequisite for the agent-cache work below.** That predicate fired correctly but could never hit, because the process was always cold — promoting more tool families was worthless without affinity first.

---

## Artifact turns can use the agent cache

`get_agent` bypassed the agent cache for *any* `extra_tools`, standing in for "this agent captured something the cache key doesn't describe." That was true for two builders and false for everything else, which closes over only session and user — both already key elements. The blanket predicate reached **76% of sessions and 95% of spend**, making every one of those turns pay a full `initialize()` plus an AgentCore Memory restore.

It is now a predicate over what a turn actually captured, starting with `create_artifact` alone — deliberately the single-builder experiment, because artifacts are the clean arm (no `assistant_id`, no memory binding) and the bypass→cache-write correlation is otherwise confounded by workload.

### Backend
- `apis/shared/tools/injected.py` (new) — the eligibility predicate, with structured `agent_cache` hit/miss logging to instrument the experiment's "initialize() per turn" gate
- `apis/inference_api/chat/service.py` / `chat/routes.py` — the MCP App dispatch paths call `get_agent` with no injected tools but the *same cache key* as real turns. They couldn't collide while artifact turns never cached; now they share a slot, so an App call arriving first would leave every later real turn hitting an agent with no `create_artifact`. Those two callers now **read** the slot but never seed it
- Needs no cache-key or `PausedTurnSnapshot` change — artifact closures are already fully described by the key — so the paused-agent orphaning hazard is not in play
- Kill switch `AGENT_CACHE_INJECTED_TOOLS_ENABLED=false` restores the blanket bypass exactly

**The honest read:** the cost thesis behind this work is disproven. It buys the ~19% marginal latency delta above, not a cache-write reduction. That is recorded in the spec rather than quietly dropped.

---

## Naming the cache miss that reports as a hit

A nonzero `cacheRead` was treated as proof the prefix was cached. So any call that read a leading segment and re-wrote everything behind it classified as `hit` with `wastedUsd = 0`. The 2026-08-05 compaction spiral did exactly that on **56 consecutive calls** — 11k read against 190k written, an 18:1 ratio — and spent 90% of a user's monthly quota invisibly.

`partial_miss` splits "read the prefix, wrote the tail" from "read a sliver, wrote the prefix": write greater than 3× read against a live entry, TTL-gated exactly like `miss_avoidable` so a legitimately cold prefix is not booked as waste. It is priced identically to a full miss, minus the tokens the call actually read.

### Backend
- `apis/shared/observability/prompt_cache.py` — the classification; `emf.py` — `PartialMiss` and `PartialMissUsd`
- `apis/shared/sessions/metadata.py` — `partialMissCount` / `partialMissUsd` session rollups, carried into `wastedUsd` on the `C#` row
- `apis/app_api/admin/costs/` — the cost-anatomy endpoint reports the new status

### Frontend
- `session-cost-anatomy.page.ts` — partial miss rendered alongside the existing statuses, so the anatomy page stops implying a clean prefix

### Infrastructure
- `prompt-cache-observability-construct.ts` — a partial-miss widget, and the platform's **first per-session alarm**: $5 of partial-miss waste in 24 hours, off the running rollup total. The fleet sums sitting beside it never saw the incident at all

Rides the existing `PROMPT_CACHE_OBSERVABILITY_ENABLED` kill switch. No new flag, **no change to any request sent to Bedrock**, and no backfill — rows written before this ships still say `hit`.

---

## Quota runway

In the 2026-08-05 incident every quota warning — 80% and 90% — fired on the day the block landed, and nothing ever told the user that **one conversation** had spent $28 of their $30 month. Two signals address that, behind one kill switch (`QUOTA_RUNWAY_ENABLED`, default on).

**Earlier rungs.** 50% and 75% join the tier's soft limit and 90%, tier-configurable via `earlyWarningPercentages` (`[]` opts out). Strictly additive — no tier loses a warning it had.

**A per-conversation notice.** The new `quota_session_notice` SSE event fires when a single conversation reaches the tier's `sessionNoticePercentage` share of the monthly limit (default 25%, 0 disables). It reads the `totalCost` already denormalized on the session row, so there is no new aggregation and no new index.

The acceptance test is a replay, not a claim: `tests/shared/test_quota_runway.py` runs the incident session's own 105 recorded cost rows (a content-free fixture — timestamp and cost only) through the ladder. **The result corrected the spec.** The session notice fires on 2026-08-01 as predicted, but the 50%/75% rungs land on 2026-08-03 UTC — a day later than expected, buying only about 6 hours over today's 80% rung. The runway comes from the per-session signal, because the defect was never that the *user* was overspending.

### Backend
- `agents/main_agent/quota/thresholds.py` (new) — the ladder; `quota/checker.py` and `quota/event_recorder.py` — a durable `session_notice` quota event on first crossing, for support
- `apis/app_api/admin/costs/` — `GET /admin/costs/top-sessions`, assembled by fanning out over the period's top-cost users rather than scanning. It reports `usersScanned` and `truncated` so a bounded list never reads as exhaustive
- `apis/app_api/admin/quota/models.py` — fixes `QuotaTierUpdate` silently dropping `softLimitPercentage` and `actionOnLimit`, which the SPA's edit form has been sending all along

### Frontend
- `quota-warning-banner.component.ts` / `quota-warning.service.ts` — the notice renders as its own dismissible chip, **scoped to the conversation it names** and never shown above another thread's composer
- `top-sessions-table.component.ts` — the new admin table; `tier-detail.component` — the two new tier settings

---

## 🐛 Bug fixes

- **A tool marked public was listed for everyone and then refused at use.** `isPublic` was read by exactly one function — `ToolCatalogService._compute_granted_by`, which builds the tool picker — while every enforcement path resolved access from role `grantedTools` alone. A public tool granted by no role therefore appeared in the picker and failed at the gate: agent tool bindings raised a hard `AgentBindingBlockedError` refusing the entire turn ("…isn't available to your account"), schedules and "Run now" dropped it silently, and `ToolAccessService` carried a second, narrower copy of the same rule. Only `"*"` holders were unaffected, which is why it read as a permissions misconfiguration rather than a bug. **In production this stranded `create_word_document`, `create_powerpoint_presentation` and `excalidraw`** — all `isPublic: true`, all granted by zero roles — including on public marketplace agents that bind them. Every gate now routes through one `AppRoleService._tool_grant_set` (role grant ∪ public tools), backed by a `get_public_tool_ids()` TTL snapshot that shares its catalog read with `get_all_tool_ids()`. The public set is unioned **at the predicate**, not merged into `UserEffectivePermissions` — that object is per-user cached and its `tools` list reaches the model's `toolConfig`, where an order flip re-writes the prompt-cache prefix. This is the tools-axis twin of the `_grants_access` consolidation already done for models
- **One leading space silently downgraded delegated identity to anonymous.** A pasted `token_exchange_audience` reached DynamoDB at 37 characters; the token service compares against its per-client allowlist ordinally and refused every exchange. The tool still *appeared* to work — the endpoint it called allows anonymous access, so the request went unauthenticated and returned plausible results. Eight refusals in the token-service log; nothing wrong in the agent's answer. A before-validator now strips surrounding whitespace and maps blank to `None`; blank → `None` matters as much as the strip, because an empty string reads as "exchange configured" and would send an empty audience. **Note for whoever hits this next:** correcting the stored value alone was not enough in dev — the runtime caches MCP clients keyed on the tool's `updatedAt` and the token-provider closure captures the audience at construction, so a fixed record is ignored until the version changes
- **Auth-mode validation held on create and not on update.** `update_tool` never considered `token_exchange_audience`, so creating a tool with an audience was checked and editing one to add an audience was not — reachable straight through the admin UI. An edit could leave a tool with an audience plus `forward_auth_token`, or an audience with MCP auth type `aws-iam`. Neither is a credential leak: the exchange branch is evaluated before forward-auth so the raw Cognito token is never sent, and with `aws-iam` the bearer takes precedence so the request reaches an IAM-expecting endpoint unsigned. Both are a broken tool. The bug is the inconsistency — a rule the UI implies was checked and wasn't is worse than no rule
- **The Runtime construct was one environment variable over a hard limit.** `AWS::BedrockAgentCore::Runtime` accepts at most 50 and the construct sat at exactly 50; the quota runway's kill switch made 51 and broke the dev Platform Stack deploy (`maximum size: [50], found: [51]`). CloudFormation enforces this when it validates the changeset — **not** at `cdk synth` — so tsc, jest and CI were all green on the PR that broke it. The variable is removed and the kill switch is unaffected, since `quota_runway_enabled()` reads `os.environ` and defaults on. Setting it to a non-default value now requires an out-of-band Runtime update until a slot is freed
- **Three dashboard widgets queried a log group nothing ever wrote to.** The inference construct declared `/aws/bedrock-agentcore/runtimes/<prefix>`; measured in dev, that group holds 0 bytes while the service's own holds 229 MB. Empty results read as "no errors" and "no traffic" rather than as a broken query — so the `cacheStatus` widget has never shown data since it shipped, and the partial-miss widget above copied the pattern before this was caught. AgentCore names the group after the AWS-assigned runtime *id* plus the endpoint qualifier, so it is knowable only from the Runtime resource
- **Two local-dev scripts tripped the `SKIP_AUTH` CI guard** — six matches, in comments, in error messages, and in the very grep patterns they use to *detect* the bypass in a developer's `.env`. Neither script sets it, so the guard's intent was never violated, but the match is real and CI is right to be blunt. Fixed in the scripts rather than by excluding `scripts/local-dev/` from the scan, which would trade a permanent hole in the guard for a cosmetic problem. Side benefit: the new character-class pattern also matches `SKIP_AUTH = true` with spaces, which the previous literal missed — so `start-app-api.sh` would have allowed a non-loopback bind for a spaced assignment

---

## 🏗️ Infrastructure

- `runtimeLogGroupName` is exposed from the inference construct and threaded to the prompt-cache observability construct, which moves into the compute-wiring phase to receive it. The phantom `LogGroup` and its retention policy — which never applied to anything — are dropped. **Operators:** retention on the real, service-created group is unmanaged. That is a live cost item, noted in the code and tracked as a W5 follow-up
- New `PartialMiss` / `PartialMissUsd` EMF metrics, a partial-miss dashboard widget, and a per-session partial-miss alarm on the `AgentCoreStack/PromptCache` namespace
- **No GSI changes.** `infrastructure/gsi-inventory.json` is byte-identical to `main`, so the one-GSI-per-`UpdateTable` limit is not in play for this release

---

## 🔧 CI/CD

- `infrastructure/test/runtime-env-var-limit.test.ts` asserts the Runtime environment-variable count stays at or under 50, and fails with a message naming the ways to free a slot. It logs remaining headroom on every run — **currently 0 free** — and synthesizes the worst case deliberately, with `tokenExchange` enabled because it spreads in three more variables. Without that the test counts 47 while prod and dev deploy 50, and the guard would report headroom no real environment has. Any future conditional block must be enabled there too, or the guard quietly stops guarding

---

## 📚 Docs

This release carries an unusually large specification arc, because the cost work is being designed in the open before it is built:

- `docs/one-pagers/cost-effectiveness-roadmap.md` — the plan of record over the whole arc, with W1–W5 workstreams and G0–G3 gates
- `docs/specs/compaction-over-threshold-cache-spiral.md`, `docs/specs/agent-cache-extra-tools-bypass.md`, compaction v2's versioned frozen prefix segments, and the document-context-offload spec with its adversarial validation and eval design
- `docs/one-pagers/fleet-prefix-spend-anatomy.md` and `backend/scripts/scan_fleet_prefix_spend.py` — measuring where model spend actually goes, across all conversations rather than one
- Two corrections worth flagging, both recorded rather than quietly fixed: the agent-cache bypass's cost thesis is **disproven** by its own gate read, and the 1.13.0 note claiming that reaping a microVM risks context loss was wrong — history restores from AgentCore Memory at agent init, so a reap is a cold start, not a loss
- `AGENTCORE_GATEWAY_TOKEN_EXCHANGE_PLAN.md` corrected: the Gateway does not perform the exchange

---

## 🚀 Deployment notes

**Deploy order: `platform.yml` → `backend.yml` → `frontend-deploy.yml`.** Unlike 1.13.0, this release **does** change `infrastructure/lib/` and requires a CDK deploy.

- **No data migration and no GSI operations.** The GSI inventory is unchanged from `main`, so nothing in this release is exposed to the one-index-per-`UpdateTable` limit
- **Everything new is behind a default-on kill switch.** `AGENTCORE_RUNTIME_SESSION_AFFINITY_ENABLED`, `AGENT_CACHE_INJECTED_TOOLS_ENABLED`, `QUOTA_RUNWAY_ENABLED`, and the pre-existing `PROMPT_CACHE_OBSERVABILITY_ENABLED` each disable their feature exactly, with no partial state
- **`QUOTA_RUNWAY_ENABLED` has no Runtime environment-variable slot.** The Runtime construct is at its 50-variable ceiling with **0 headroom**, so the flag is read from `os.environ` with a default of on. Turning it *off* requires an out-of-band Runtime update until a slot is freed — and any new Runtime variable added from here requires retiring an existing one first, which `runtime-env-var-limit.test.ts` will now tell you in CI rather than at deploy
- **Expect fewer cold starts and faster turns after the first in each conversation** once session affinity is live. This partially offsets 1.13.0's idle-reaper change, which deliberately shrank the warm-microVM pool: conversations now reuse their own container rather than relying on a large shared pool
- **Users on tiers with default settings will start seeing quota warnings at 50% and 75%**, and a per-conversation notice at 25% of the monthly limit. Both are tier-configurable — set `earlyWarningPercentages` to `[]` and `sessionNoticePercentage` to `0` to keep the old behaviour for a given tier
- **Watch the new per-session partial-miss alarm.** It is the first alarm on this platform that fires for a single conversation rather than a fleet aggregate, and it exists because the fleet sums did not see the incident that motivated it
- **The terminal client deploys nothing.** `tui/` is a standalone `uv` project distributed via `uvx agentcore-tui`; it is not part of any image, and it needs an API key from the existing API-key feature

---

# Release Notes — v1.13.0

**Release Date:** August 2, 2026
**Previous Release:** v1.12.3 (August 1, 2026)

---

> 🖥️ **Backend + frontend deploy** — run `backend.yml`, then `frontend-deploy.yml`. **No CDK deploy**, no new AWS resources, no configuration change, no data migration. There are zero `infrastructure/lib/` changes in this release, so `platform.yml` has nothing to apply.

---

## Highlights

Two things in this release were quietly costing us, in opposite currencies.

A **published Agent listing was a dead end**. Version snapshots made what users run immutable — which is the point — but they also meant an author's edits landed on a draft that reached nobody, and `published` had no edge back into review. Fixing a typo meant asking for the Agent to be taken off the shelf. This release adds the update path: authors submit an update to a live listing, and the approved version keeps serving every user for the entire review.

Meanwhile the **AgentCore Runtime's idle reaper had been disabled since May** — not by configuration, but by our own `/ping` handler, which refreshed its idle timestamp on every poll and so reported a microVM as busy forever. Every microVM ran its full 8-hour `maxLifetime` no matter how long ago its last turn ended. In July that was 71,954 microVM-hours at 1.23% CPU utilization to serve 9,568 turns — **73% of the platform bill**. The handler now reports the contract AgentCore actually reads.

Alongside those, **three Lambda images that had been failing at import on every single invocation** are fixed — document ingestion and scheduled KB sync both come back — and the DynamoDB GSI limit that took production down on 2026-08-01 gains a pre-merge CI guard so no future release can repeat it.

---

## Shipping an update to a live listing

An author with a published Agent could not get a change to their own users. Their edits went to the draft; the store served the approved snapshot. The only routes were to request withdrawal — taking the Agent off the shelf to fix a typo — or to wait for an admin to send it back with `request_changes`, which is not the author's to start.

The listing state machine now has a `published → in_review` edge.

This is not a second door into the store. `submit_listing` carries `published_version` and its index key through untouched, so the approved snapshot keeps serving for the whole review, and approval remains the only edge that changes what users get. `AUTHOR_TARGET_STATES` deliberately does **not** gain `published` — widening it would let an author walk their own unreviewed first submission onto the shelf.

Cancelling an update is the subtle half. Withdrawal picks its target from `is_on_shelf`, so cancelling a pending update previously read as the author asking to delist — a request they never made, parked in an admin's queue. A submission made over something already on the shelf now records its origin, and cancelling derives the target from that rather than accepting one. Assuming `published` instead would discard an outstanding change request and publish something no admin approved.

### Backend
- `apis/shared/assistants/listing.py` — `published → in_review` in `ALLOWED_TRANSITIONS`; `AgentListing.submittedFrom` records a submission's origin state
- `apis/app_api/agent_designer/services/listing_service.py` — cancel derives its target from `submitted_from`, the same mechanism as `withdrawal_from`; an author's silence on `publisherId` now keeps the listing's **current** publisher rather than resolving back to their individual profile, so an update no longer silently undoes a D12 reattribution
- `apis/shared/assistants/models.py` — `publishedVersion` and `submittedFrom` on the wire

### Frontend
- `share-agent-dialog.component.ts` / `submit-listing-dialog.component.ts` — "Submit an update" / "Cancel update" wording, driven by whether a listing is already live
- `listing-status.component.ts` — a line on the status badge stating that the published version stays live during review

### Test Coverage
226+ lines in `test_agent_listing.py` and 105+ in `test_agent_listing_state_machine.py`, plus SPA specs for both dialogs.

---

## The idle reaper, re-armed

`/ping` returned `time_of_last_update: int(time.time())` on every poll. AgentCore measures idleness as `now - time_of_last_update` and polls roughly every two seconds — so the reported idle time was never more than about 2 seconds, and the 900-second `idleRuntimeSessionTimeout` could never fire. Every microVM instead ran to `maxLifetime`: 8 hours, however long ago its last turn finished.

The cost is measurable and large. Mean microVM life stepped from 21.5–33.6 minutes (May 13–14) to 488–496 minutes from May 28 onward, exactly at the deploy of #338 which introduced the moving timestamp. July: 9,568 turns, 71,954 microVM-hours, 1.23% CPU utilization, 217,806 GB-hours — about $2,058, or 73% of the platform bill. On August 1, 88 of 89 microVMs served exactly one turn and then sat idle for eight hours.

### Backend
- `apis/inference_api/runtime_health.py` (new) — reports the contract the upstream SDK implements: `HealthyBusy` while a turn is in flight, `Healthy` with a **frozen** timestamp once idle, so idle time actually accrues and the reaper fires on schedule
- `apis/inference_api/main.py` — registers `InvocationActivityMiddleware`, which is **pure ASGI rather than `BaseHTTPMiddleware`**. The unit of work is the streamed SSE body, not the handler call: `BaseHTTPMiddleware` hands back control the moment the handler returns a `StreamingResponse`, before the agent has produced a token. A pure-ASGI `await self.app(...)` does not return until the body is fully sent, so `try/finally` spans the real turn and cannot leak the counter on client disconnect or handler error

Two deliberate deviations from the upstream SDK, both protecting #338's original fix:

- The timestamp refreshes on every poll **while busy** rather than freezing at turn start, so a turn running past `idleRuntimeSessionTimeout` is never reaped mid-stream even if the platform reads only the timestamp and ignores the status (`bedrock-agentcore-sdk-python#471`)
- Transitions are stamped by `enter()`/`exit()` rather than sampled when a poll happens to observe them. The SDK infers them inside its ping handler, so a turn shorter than the poll interval is never seen as activity — leaving the idle clock running from process start, which can strand a just-served session one poll away from a reap

### Infrastructure
None. The runtime uses AWS's default lifecycle values; this was never a configuration problem.

### Test Coverage
268 lines in `test_runtime_health.py` covering the ping contract, busy/idle transitions, and the middleware's `try/finally` spanning the streamed body.

---

## Three Lambda images were failing at import

`apis/shared/timestamps.py` landed in `8544d87a` and is imported by `documents/ingestion/status.py`, but neither `Dockerfile.rag-ingestion` nor `Dockerfile.kb-sync` copied it. Both images had been raising `ModuleNotFoundError` on **every invocation** ever since — rag-ingestion and both kb-sync Lambdas at a 100% error rate in dev since roughly 2026-07-27, and in prod since the 2026-08-01 18:46 UTC deploy.

Nothing surfaced it. The S3 events fired, the crawler staged its markdown correctly, and the container died at import before any handler code ran. Documents simply sat at `uploading` forever, and scheduled KB sync stopped running.

Two more gaps of the same shape were already present and are closed here:

- **kb-sync** omitted `assistants/` and `dynamo_errors.py`, reached from `document_service.soft_delete_document` through a `try/except ImportError` that logs "boto3 is required" and returns `None` — so the worker's miss-eviction path silently no-opped rather than crashing
- **scheduled-runs** omitted `errors.py`, `storage/` and `observability/`, reached from `sessions/metadata.py`

### Test Coverage
216 lines in `test_lambda_image_imports.py`, which walks each image's real import graph from its handler entrypoints and fails on anything not COPYed. It deliberately does **not** distinguish module-level imports from function-local ones — `handler.py` reaches `status.py` via a function-local import, so a module-level-only check would pass the very bug this fixes. It also resolves bare module names against each image's flattened COPY root, without which the walk never enters `status.py` at all.

---

## A CI guard for the GSI limit that took prod down

Release 1.12.0 added two GSIs to the existing `{prefix}-rag-assistants` table in a single CloudFormation update. DynamoDB's `UpdateTable` API permits exactly one GSI creation or deletion per call, so the update failed and CloudFormation rolled back every other resource in the deploy with it — including a brand-new audit-log table. `backend.yml` and `frontend-deploy.yml` succeeded independently, leaving production running new code against old infrastructure with the agent store returning 500. Recovery took two more patch releases.

Dev could not have caught it. The two indexes reached `develop` in separate merges, so dev got a platform deploy for each and never saw them collapse into one update. Only an environment that jumps a whole release at once is exposed — which means no amount of soak time would have surfaced it.

The limit applies to `UpdateTable` only; `CreateTable` accepts any number of indexes, which is why the new audit-log table's two GSIs were never the problem. The right question is therefore *"does any **existing** table gain more than one GSI?"*, not *"are there new indexes?"*.

- `infrastructure/gsi-inventory.json` — a committed, synth-generated inventory of every DynamoDB table and its indexes
- `infrastructure/test/gsi-update-limit.test.ts` — synthesizes `PlatformStack` and fails if the committed inventory has drifted from the CDK code, so the inventory can never go stale silently. Regenerate with `UPDATE_GSI_INVENTORY=1 npx jest gsi-update-limit`
- `scripts/release/check-gsi-update-limit.mjs` — dependency-free diff of the inventory between `origin/main` and the PR. Exempts tables present only on the branch (`CreateTable`) and tables dropped entirely (`DeleteTable`); counts creations and deletions together, since the API limit is one GSI *operation* per update, not one addition
- `.github/workflows/gsi-update-limit.yml` — runs on PRs into `main` only. Diffs two committed JSON files: no `npm install`, no CDK synth, a couple of seconds

Verified by replaying the real incident: the 1.12.0 inventory delta fails the check naming both indexes, the 1.12.1/1.12.2 split passes, and the new audit-log table is correctly exempt.

> **Note for this release only:** the automated check reports **SKIPPED**, because `main` has no `gsi-inventory.json` to diff against until this release lands. The prerequisite was verified by hand instead — there are **zero** `infrastructure/lib/` changes in this release, so no table gains or loses any index. From 1.13.1 onward the check runs for real.

---

## Bug fixes

- **The submit dialog showed authors the wrong category.** The listing's category was preselected into `category()`, but the select rendered `Administration` — the first option — so an author updating a `Student Support` listing saw a shelf they never chose, reading as though submitting had moved it. The cause is `[value]` on the `<select>`: the whole form sits behind `@if (loading())`, so the select and its options render in one pass, and Angular applies an element's own bindings before creating its children. `select.value` was set while the select still had no options, the browser dropped it, and the first option won. Binding `[selected]` on the options cannot lose that race. Display-only — `category()` held the right value throughout, so submissions always sent the correct shelf. The hazard ran the other way: an author who *wanted* `Administration`, saw it already selected, and submitted something else. Pre-existing but only reachable from `changes_requested` and `taken_down` until this release made updating a live listing the common path (#828)
- **The agent store and admin problem-report queue no longer return 500 when a GSI is absent.** An absent index is a legitimate transient deploy state, not a programming error — CloudFormation reports success while a GSI is still `CREATING`, deploys can roll back, and `platform.yml` and `backend.yml` are separate workflows that can land out of order. All three converged on 2026-08-01, and the agent store returned 500 to every user for the two releases it took to repair the infrastructure — in the release that opened the store's navigation to GA. Both reads now catch the missing-index case specifically, log at WARNING with the index name, and return an empty result with no cursor. The match is narrow on purpose: catching every `ValidationException` would hide malformed key conditions and bad cursors behind a permanently empty surface, and catching every `ClientError` would turn throttling into "there is nothing here". `DueSyncIndex` on the KB-sync dispatcher is deliberately left loud — a background sweep that silently returns nothing means scheduled syncs stop with only a warning. Writes and admin mutations are untouched and still fail loudly (#822)
- **Deleting a taken-down Agent no longer requires a round trip through the review queue.** `delete_assistant` refuses every listing state but `private` and told the author of a taken-down Agent to "take it back to private first" — advice for a door that did not exist. The only route out was to resubmit for review and withdraw a moment later: a junk entry in the admin queue as the price of a delete. The edge is added rather than the refusal reworded, because the audit-grounds justification never held — `taken_down → in_review → private` was always walkable by the author alone. Safe in the direction that matters: `is_on_shelf` hardcodes `taken_down` to `False`, so this can never route to `withdrawal_requested` or pull something off a shelf it is not on, and approval is still the only door into the store (#824)
- **`heroBookmark` had nothing to resolve to** on the Pinned page's empty state — `pinned.page.ts` imported `provideIcons` and `heroBookmark` and called neither, and the SPA registers icons per-component with no root-level registration (#815)

---

## The agent launch card, simplified

The card is read at the moment of typing, and two of its blocks were restating things the user had already been told on the way in: the capability chips duplicated the detail page's "what it can access", and "Ready to run for you." was a green line announcing that nothing happened. Both are gone, along with `capabilities` on the view model — the projection was its only writer.

Only the blocked half survives. "You can't run this and here is what's missing" is news a user can act on; "this works" is not. The footer bar now renders only for a blocked verdict or the Agent details link, so a healthy private Agent has no footer at all.

Attribution reads "By &lt;name&gt;" rather than a bare name, which under a description read as a caption instead of an author. The name carries the weight and "By" stays muted, so the eye lands on who made it. A verified publisher keeps its treatment and still outranks `ownerName` outright — on a published departmental Agent the name to trust is the department, not whoever holds the record. The "By" is dropped when there is no name at all, so a category-only Agent never renders a dangling preposition (#825)

---

## 🔒 Security

Ten `py/log-injection` findings CodeQL raised against the 1.12.0 delta are closed, plus three adjacent findings that turned out to be real defects rather than dead code (the missing icon registration and the `_ROLE_GATED_KINDS` drift above).

`scrub_log` (`apis/shared/security/log_sanitize.py`) already existed and was used in six files; the marketplace, audit and role-pin code shipped without adopting it. Path parameters — `agent_id`, `role_id`, `target_id`, `report_id`, `actor_user_id` — and exception text now pass through it before reaching a log message. A percent-encoded `%0A` in a path segment survives URL decoding, and most of these sites sit in `except` blocks where the value was never validated, so a forged log line was reachable.

Scope note: only the message f-strings are scrubbed, not the `extra={...}` structured fields CodeQL also flags. `logging.basicConfig` formats records as `%(asctime)s - %(name)s - %(levelname)s - %(message)s`, so `extra` is never rendered — scrubbing it would corrupt the stored value for any future handler that *does* read it, and buy nothing against injection. `tool_filter.py` had the flagged warning twice in two near-identical methods and CodeQL reported one; both are fixed, since leaving the other would be arbitrary (#815)

---

## 🚀 Deployment notes

1. **`backend.yml`** — required. Ships app-api, inference-api, rag-ingestion, kb-sync (dispatcher + worker) and scheduled-runs. The three Dockerfile closure fixes only take effect once these images are rebuilt and the Lambda code is updated.
2. **`frontend-deploy.yml`** — required, for the submit/share dialogs, the listing status badge, the launch card and the Pinned page icon.
3. **`platform.yml`** — **not required.** There are no `infrastructure/lib/` changes in this release.

**What to watch after the deploy:**

- **Document ingestion and KB sync recover on their own.** Documents stuck at `uploading` were never ingested; re-upload or re-trigger a sync for anything staged during the outage window (dev from ~2026-07-27, prod from 2026-08-01 18:46 UTC). No data was lost — the crawler staged its markdown correctly throughout; only the handler never ran.
- **Expect microVM count and cost to drop sharply, and expect more cold starts.** Sessions idle for 900 seconds are now reaped instead of held for 8 hours, which shrinks the pool of warm microVMs. A user returning after 15 minutes gets a cold start where they previously landed on a warm instance.
  **Conversation history survives a reap.** It is restored from AgentCore Memory at agent init, on a cold container as readily as a warm one: the SDK takes its RESTORE branch whenever `read_session()` and `read_agent()` both resolve, which they do because `persistence_mode` defaults to `FULL` and `agent_id` is always Strands' default. Those reads are `list_events` calls keyed on memory/actor/session and carry no in-process state, so restore does not depend on the agent cache surviving. The `Restore @init: N message(s)` line in the runtime log group reports what each init actually loaded — a useful one-query check if this is ever in doubt.
  The real cost of a reap is therefore container init latency plus a fresh Bedrock prompt-cache write, since a cold restore re-serializes history through the sanitizers and pairing repair while the warm path only appends. At a 900-second idle gap the prompt cache has almost certainly expired regardless.

---

# Release Notes — v1.12.3

**Release Date:** August 1, 2026
**Previous Release:** v1.12.2 (August 1, 2026)

---

> 🖥️ **Frontend only** — run `frontend-deploy.yml`. No CDK deploy, no backend change, no configuration change, no data migration.

---

## Highlights

Removes a spurious error dialog that appeared on **every page load for every user** in any environment where Memory Spaces is turned off — which today means production.

The underlying 404 was correct and intentional. Memory Spaces is deliberately hidden in production, and the way it hides is by returning 404 across its whole API surface, so the feature can be switched off without being torn out. The app already understood that: it read the 404 as "feature unavailable" and quietly dropped the sidebar entry. What it did not do was tell the global error handler to stay quiet, so the feature hid itself and then popped a dialog naming an endpoint nobody was meant to see.

Nothing about the feature's availability changes. Memory Spaces remains off in production.

## Fixed

- **No more `404 /memory/spaces` dialog.** `SUPPRESS_ERROR_TOAST` is now set on every Memory Spaces request. Genuine failures still surface — they always went through `MemorySpaceService`'s own error state, which the Memory Spaces pages render inline and in context; the generic dialog was a duplicate of that even when the feature was on (#819)

## Why it appeared now

The switch that hides Memory Spaces in production is applied at **deploy** time, and 1.12.0's CDK deploy rolled back. The first deploy to actually carry it into production was 1.12.1. So the dialog is not a regression in 1.12.2 — it is the first time the production kill switch has been live, and this is the rough edge it exposed.

## Verification note

This fix is invisible in any environment where Memory Spaces is **enabled**, including dev, because nothing 404s there. It is covered by unit tests that pin the interceptor contract directly rather than by a manual check.

---

# Release Notes — v1.12.2

**Release Date:** August 1, 2026
**Previous Release:** v1.12.1 (August 1, 2026)

---

> 🏗️ **CDK deploy required** — `platform.yml` only, and **only after 1.12.1's `AgentDirectoryIndex` reports `ACTIVE`**. Deploying before that reproduces the 1.12.0 failure exactly. Backend and frontend are unchanged from 1.12.0.

---

## What this release does

Completes the deploy 1.12.1 split in half. It restores `AgentReportsIndex` — the GSI backing the admin problem-report queue — which 1.12.1 deferred so that release could add a single index and satisfy DynamoDB's one-GSI-per-`UpdateTable` limit.

The index definition is unchanged. This is the byte-for-byte inverse of 1.12.1's removal, so the construct returns to matching `develop` exactly and the two branches stop diverging.

**The admin problem-report queue works again.** It had been returning 500 since 1.12.0, because the code that reads the index shipped while the deploy that would have created it rolled back. Reports users submitted during the gap were written normally and none were lost — only the admin view that reads them was unavailable.

## Before deploying

Confirm 1.12.1's index finished backfilling. CloudFormation reports success while an index is still `CREATING`, and starting this deploy against a `CREATING` index puts two index operations in flight and fails the same way 1.12.0 did:

```bash
aws dynamodb describe-table \
  --table-name {prefix}-rag-assistants \
  --query 'Table.GlobalSecondaryIndexes[].{Name:IndexName,Status:IndexStatus}' \
  --output table
```

Five indexes, all `ACTIVE`, `AgentDirectoryIndex` among them. Then run `platform.yml`. Afterwards the table should read six.

Nothing else to run. No data migration.

## Note for operators

The 1.12.0 → 1.12.1 → 1.12.2 sequence exists because **1.12.0 added two GSIs to an existing table in one CloudFormation update**. `UpdateTable` permits exactly one GSI create or delete per call. `CreateTable` has no such limit, which is why a brand-new table with several indexes deploys without trouble.

The trap is that this is invisible in any environment that took the changes incrementally. Both indexes reached `develop` in separate merges and so got a deploy each; only an environment jumping a whole release at once sees them collapse into a single update. When cutting a release, the question to ask is not "are there new indexes?" but **"does any *existing* table gain more than one?"** — and if so, split the deploy before merging rather than after a rollback.

---

# Release Notes — v1.12.1

**Release Date:** August 1, 2026
**Previous Release:** v1.12.0 (August 1, 2026)

---

> 🏗️ **CDK deploy required** — this release exists to make 1.12.0's CDK deploy land. Deploy `platform.yml` only; the backend and frontend are unchanged from 1.12.0 and already shipped. **A second deploy follows in 1.12.2** — see below.

---

## What happened

1.12.0's `platform.yml` deploy failed and rolled the PlatformStack back. The `backend.yml` and `frontend-deploy.yml` deploys both succeeded, so production spent the gap running 1.12.0 code on 1.11.1 infrastructure — the agent store returning 500 on a `AgentDirectoryIndex` that did not exist yet.

The cause: 1.12.0 adds **two** GSIs to the existing assistants table in a single CloudFormation update — `AgentDirectoryIndex` for the marketplace store and `AgentReportsIndex` for the problem-report queue. DynamoDB's `UpdateTable` permits exactly one GSI create or delete per call, so CloudFormation failed on that resource and rolled back everything else with it, including the new audit-log table.

The 1.12.0 deploy note called for one new GSI. There were two. They arrived on `develop` in separate merges and so got a platform deploy each, which is why no non-production environment ever saw the limit — only an environment jumping the whole release at once does.

The restriction applies to `UpdateTable` alone. `CreateTable` accepts as many indexes as you like, which is why the brand-new audit-log table's two GSIs were never the problem.

## What this release does

Defers `AgentReportsIndex` so the deploy carries a single GSI addition. Nothing else changes — no application code, and the index definition itself is untouched.

This deploy also re-applies everything the rollback reverted: the `audit-log` table and its app-api wiring, the optional token-exchange secret, and the Gateway construct changes. **The agent store recovers here.**

## Deploying

Run `platform.yml`. Then wait for the index to finish backfilling before deploying 1.12.2 — CloudFormation reports success while the index is still `CREATING`:

```bash
aws dynamodb describe-table \
  --table-name {prefix}-rag-assistants \
  --query 'Table.GlobalSecondaryIndexes[].{Name:IndexName,Status:IndexStatus}' \
  --output table
```

Every index should read `ACTIVE`, `AgentDirectoryIndex` among them. On a table of a few thousand agents this takes a couple of minutes.

Nothing else to run: `backend.yml` and `frontend-deploy.yml` are unchanged from 1.12.0. No data migration.

## Known issue

The **admin problem-report queue returns 500** until 1.12.2 deploys, since it queries the deferred index. User-submitted reports are still written and are not lost — only the admin queue that reads them is unavailable.

Nothing else regresses. Audit writes fail open by design: `AuditService` checks for the table environment variable and falls back to structured logs, so no administrative mutation fails while the table is absent.

---

# Release Notes — v1.12.0

**Release Date:** August 1, 2026
**Previous Release:** v1.11.1 (July 24, 2026)

---

> 🏗️ **CDK deploy required this release** — one new DynamoDB table (`{prefix}-audit-log`), one new GSI on the assistants table (`AgentDirectoryIndex`), and an optional Secrets Manager secret. Deploy order is `platform.yml` → `backend.yml` → `frontend-deploy.yml`. **Wait for the new GSI to reach `ACTIVE` before shipping the backend** — CloudFormation reports success while the index is still backfilling, and the marketplace store queries it. No data migration.

---

## Highlights

This release turns Agents from a personal authoring tool into a **governed institutional catalog**.

The **Agent Marketplace** ships GA. An author submits their Agent from the Designer, an admin reviews and approves it, and it appears on a browsable store with categories, a curated front, icons and problem reports. Approval means something durable now: every submission freezes an **immutable version snapshot**, and both the store and the invocation path serve *that* snapshot — so an author editing after approval can no longer silently change what the institution is running. Admins can roll a listing back to any earlier approved version, see a diff of exactly what a submission changed, and read `publishedVersion` to know exactly which snapshot the store is serving.

Governance grew to match. **Delegated admin scopes** let a system admin hand out a single admin area — Cost Analytics, Tools, Marketplace — without handing over the whole console, across 16 scopes with three (`admin.roles`, `admin.auth_providers`, `admin.audit`) permanently non-delegable and no wildcard on the axis. Every role mutation, including denied ones, now lands in a durable **audit trail** with its own admin page.

Two integration paths open up for forks and downstream teams. An AgentCore Gateway can authenticate inbound calls with a **Cognito JWT** instead of SigV4, so Gateway targets can act per-user. And the agent can perform an **RFC 8693 token exchange**, trading the signed-in user's token for one issued by a token service the organization already runs — letting existing internal APIs serve agent traffic as the user with no change on their side.

The Assistant → Agent rename completes: the Assistant editor is retired and `/assistants` becomes an explainer. **Action required:** a CDK deploy, and forks running SigV4 Gateway callers should read the breaking-change note before touching `CDK_GATEWAY_INBOUND_AUTH` — a Gateway's authorizer is immutable after creation.

---

## Agent Marketplace

Agents can now be published to an institution-wide store, reviewed by an admin, and discovered by everyone else. Previously an Agent was reachable only by its author and whoever they shared a link with; there was no catalog, no review, and no way for an admin to know what was in circulation.

### Backend

- `apis/shared/assistants/listing.py`, `listing_repository.py`, `storefront.py`, `categories.py`, `publishers.py`, `reports.py` — the listing lifecycle (draft → submitted → approved → published → withdrawn/taken down), category and publisher records, and the storefront composition.
- `apis/app_api/agent_designer/services/` — `listing_service.py` (submit + preflight), `store_service.py` (browse), `agent_detail.py`, `icon_service.py`, `pin_service.py`, `role_pin_service.py`, `report_service.py`.
- `apis/app_api/admin/agents/routes.py` — the admin surface: review queues, listings, categories, publishers, store front, reports, takedown, rollback, withdrawal decisions.
- **Runnability** is checked before a listing is offered: an Agent whose bound model, tool, skill or memory space is missing is `blocked`, and the reviewer is told when a listed Agent is unopenable rather than discovering it after publication.
- **Icons** (`apis/shared/assistants/icons.py`) — bytes live in S3 under a content-addressed key `assistants/{agent_id}/icons/{sha256[:16]}.{png|jpg}`; the digest doubles as the ETag and the `?v=` cache version, so icons cache `immutable` and still change on upload. Uploads are format-sniffed (the `Content-Type` header is ignored) and **always re-encoded**, which strips EXIF — an author uploading a phone photo would otherwise publish its GPS coordinates institution-wide.
- **`@`-mention** (`apis/inference_api/chat/agent_binding_policy.py`) — mentioning an Agent from the composer hands it exactly one turn. Mention turns deliberately skip *both* halves of conversation binding: they neither validate against the session's bound Agent nor persist a new binding, so mentioning inside an existing thread is the normal case and the next unmentioned message is plain chat again. Binding is never authorization — the Agent is still resolved through `get_assistant_with_access_check` on every turn.

### Frontend

- `agents/discover/discover.page.ts` — the storefront-style browse page, with `agent-store-tile`, `agent-spotlight` and category filtering.
- `agents/detail/agent-detail.page.ts` — the public Agent page, gated on runnability, with the instructions disclosure gate.
- `agents/pinned/pinned.page.ts` plus `agent-pin.service.ts` — user pins, seeded from role defaults that resolve live rather than being copied.
- `agents/components/submit-listing-dialog.component.ts` — the author's submit flow, with a preflight that names every blocker (including visibility) before submission and lets the author go public inline.
- `agents/components/share-agent-dialog.component.ts` — one surface for an Agent's reach: visibility, collaborators and listing state together.
- `session/components/chat-input/agent-mention-menu.component.ts` — the composer's `@`-mention picker.
- `admin/marketplace/` — Review queue, Listings, Categories, Default pins, Store front, Publishers and Reports pages, with dialogs for takedown, request-changes, rollback, withdrawal decisions and report resolution.

### Infrastructure

- New sparse `AgentDirectoryIndex` GSI (`GSI5_PK`/`GSI5_SK`, ALL projection) on `{prefix}-rag-assistants` — only listed Agents carry the keys, so unlisted Agents cost nothing.
- New CDK config `agentMarketplace.enabled` → `AGENT_MARKETPLACE_ENABLED` on **app-api only**; the marketplace adds no inference-api routes, because publication is a catalog concern.

### A note on the GA gate

The store shipped behind an `@if (showAgents() && isAdmin())` nav condition, which turned out to be the *only* closed door: `/agents/discover`, agent detail pages, the composer `@`-mention menu and role-seeded pins were all already reachable by any authenticated user. That interim state was worse than either end state, so the nav gate is now the feature flag alone. There is no RBAC capability on this axis — a capability id cannot be granted from the admin roles UI (the same defect that made the short-lived `skills` and `scheduled-runs` gates inoperable), so shipping one would have meant a gate nobody could open.

---

## Immutable Agent Version Snapshots

Approval now freezes what it approved. Before this, an admin approved an Agent and the author could edit its instructions, swap its model or add tools the next minute — users would silently get the new behavior under the old approval. Now every submission captures an `AgentVersion`, and both the store and the invocation path serve the *approved snapshot*, not the author's working draft.

### Backend

- `apis/shared/assistants/versions.py`, `version_repository.py` — `VERSION#` child rows under the Agent's own partition, capturing instructions, model, tool/skill/memory bindings.
- `apis/shared/assistants/version_resolution.py` — resolves which version a given caller should run.
- `apis/shared/assistants/version_diff.py` — computes the delta between the last approved snapshot and the submission under review.
- Rollback and roll-forward: an admin can point a published listing at any earlier approved version.
- **Drift detection was removed, not extended.** The post-approval drift marker (#757) hashed `assistant.instructions` and nothing else — it could not see a model swap, a tool change or a retargeted memory space, and its weak `edited` fallback reported an admin's own typo fix as a behavior change. Snapshots made the question moot: a published Agent is an immutable snapshot the author cannot reach, so `listing.publishedVersion` answers "is what I approved still what is live?" as a fact rather than a heuristic. `_instructions_hash` and `_drift` are deleted rather than left dormant.
  - **Transitional detail:** `AgentListing` is `extra="allow"`, so a listing approved *before* the removal still carries `approvedInstructionsHash` in DynamoDB. Nothing writes it, and it is explicitly stripped from responses for viewers who lack instructions permission — a hash of the instructions would confirm a guessed prompt. Safe to delete from stored rows once none carry it.

### Frontend

- `admin/marketplace/components/review-diff.component.ts` — the reviewer sees exactly what changed, so approval is a decision about a delta rather than a re-read of the whole Agent.
- `admin/marketplace/components/rollback-dialog.component.ts` and the version picker on the admin Listings page.
- `agents/components/listing-status.component.ts` — the author's view of listing state and which version is published.

### Withdrawal became a request

An author asks for a listing to be withdrawn rather than unilaterally unpublishing; an admin decides, the withdrawal's origin is recorded, and `delete` respects a pending request instead of racing it.

---

## Delegated Admin Scopes

A system admin can now hand out one admin area at a time. Previously the admin console was all-or-nothing: the only way to let someone manage the tool catalog was to make them a full system admin.

### Backend

- `apis/shared/rbac/admin_scopes.py` — a **closed registry** of 16 scopes (`admin.costs`, `admin.quota`, `admin.fine_tuning`, `admin.models`, `admin.tools`, `admin.skills`, `admin.connectors`, `admin.file_sources`, `admin.export_targets`, `admin.marketplace`, `admin.users`, `admin.system_prompts`, `admin.user_menu_links`, `admin.roles`, `admin.auth_providers`, `admin.audit`), grouped to mirror the admin nav.
- Scopes are defined in code, never derived from a catalog and never free text — a deliberate response to the `skills`/`scheduled-runs` capability gates, which could not be granted from the roles UI at all and were therefore inoperable.
- **Three scopes are permanently non-delegable.** `admin.roles` is self-evident: whoever can edit a role can grant themselves anything. `admin.auth_providers` is the non-obvious one — role resolution starts from JWT claims, so whoever controls IdP attribute mapping controls which AppRoles resolve; it is role administration by another route. `admin.audit` records the actions the other two take. Enforced at the service layer by `role_constraints.validate_admin_scopes`, so the rule holds for the REST API, seed scripts and future automation alike.
- **There is deliberately no wildcard.** `"*"` is the idiom on the tool, model and skill axes; here it would be shorthand for turning a role into a superuser. Full admin is spelled `system_admin`.
- New `grantedAdminScopes` axis on `AppRole`, enforced on admin routes and exposed over the API.

### Frontend

- `auth/admin-scope.guard.ts` and `admin/admin-scope.model.ts` — the admin console nav and routes gate on resolved scopes.
- The roles form renders real checkboxes from the served registry, grouped exactly as an admin already navigates the console.
- The roles list shows which roles carry delegated admin power, so it is visible at a glance rather than buried in a detail page.

---

## Administrative Audit Trail

Role mutations are now durably recorded. An admin can answer "who changed this role, when, and what did they change" — including attempts that were denied.

### Backend

- `apis/shared/audit/` — `models.py` (`AuditAction`, `AuditOutcome`, `AuditRecord`), `repository.py`, `service.py`.
- Recorded actions: `app_role.created`, `app_role.updated`, `app_role.deleted`, `app_role.synced`, and `app_role.mutation_denied` — denied attempts matter as much as successful ones.
- `apis/app_api/admin/audit/routes.py` — month-scoped browsing, per-actor lookup, and an action registry.

### Frontend

- `admin/audit/pages/audit-log.page.ts` — the audit page, with month navigation. The empty state is gated on a *successful* read, so a failed load no longer renders "Nothing recorded in <month>" underneath its own error banner and tells an admin a month is empty when nobody knows that.

### Infrastructure

- New `{prefix}-audit-log` DynamoDB table: `PK`/`SK`, `ActorIndex` (`GSI1PK`/`GSI1SK`) and `RecentIndex` (`GSI2PK`/`GSI2SK`) both ALL-projected, PITR enabled, AWS-managed encryption, TTL on `expiresAt`. Name published to `/{prefix}/audit/audit-log-table-name`.

---

## AgentCore Gateway JWT Inbound Auth

A Gateway can now authenticate inbound calls with a Cognito JWT instead of SigV4, so Gateway targets can act as the signed-in user rather than behind a single service identity.

- `config.gateway.inboundAuth` (`'iam' | 'jwt'`, default `iam`) selects the Gateway's single inbound authorizer, driven by `CDK_GATEWAY_INBOUND_AUTH`.
- The agent forwards the signed-in user's token to a JWT Gateway.
- Outbound credentials stay per-target, so one Gateway still serves both IAM-invoked Lambda targets and OAuth targets.

### ⚠️ The authorizer is immutable after creation

Changing `inboundAuth` on an **existing** Gateway does not work. The AgentCore control plane rejects it mid-deploy:

```
Authorizer type cannot be updated for an existing gateway
(Service: BedrockAgentCoreControl, Status Code: 400)
```

This is invisible to every pre-deploy check available. The CloudFormation resource reference documents `AuthorizerType` as "Update requires: No interruption", and `cdk diff` — even through a real change set — reports an in-place `[~]` modify. Both describe CloudFormation's plan, not the service's validation. **A change set is not a deploy test.**

The default therefore stays `iam`, matching every Gateway already deployed. Moving an existing deployment to `jwt` requires a *new* Gateway plus target re-registration and a cutover, not a config flip. `load-env.sh` validates the value so a typo fails the deploy rather than silently falling back.

**Forks:** a Gateway accepts exactly one authorizer type — there is no "accept either SigV4 or JWT" mode. If anything other than this stack's agent calls your Gateway with SigV4, migrate those callers before creating a JWT Gateway.

---

## RFC 8693 Token Exchange

The agent can trade the signed-in user's Cognito access token for a token issued by a token service the organization already runs, so **existing internal APIs can serve agent traffic as the user without being modified**.

- `agents/main_agent/integrations/token_exchange.py` — performs the exchange and forwards the result to the MCP server exactly as `forward_auth_token` does with a raw token. Exchanged tokens are cached per `(user, audience)` and reused until shortly before expiry; the cache holds credentials, so entries are never shared across users.
- **Why not the Gateway:** AgentCore Gateway's outbound OAuth credential provider supports only `CLIENT_CREDENTIALS` and `AUTHORIZATION_CODE` — verified against the `bedrock-agentcore-control` API model, where `OAuthGrantType` has exactly those two values and `TOKEN_EXCHANGE` appears nowhere. A token-exchange grant is not expressible there. Separately, a Gateway with `AWS_IAM` inbound auth never receives the user's token, so it would have nothing to exchange.
- **Known limitation:** revocation does not propagate. The token service validates the subject token offline (signature, issuer, expiry, `token_use`, `client_id`) and never asks Cognito whether it is still live, so a Cognito token revoked before its expiry remains exchangeable. Exposure is bounded by the token service's own lifetime cap.

### Infrastructure

- New optional `{prefix}-token-exchange-client` Secrets Manager secret holding a `client_id → client_secret` map, **populated out of band** — CDK cannot generate a credential shared with an external system.
- Entirely additive: leave `CDK_TOKEN_EXCHANGE_URL` unset and no resources, permissions or environment variables are created. A deployment that only ever uses SigV4 for MCP traffic is unaffected.

---

## The Assistant → Agent Rename Completes

One noun in the nav. The Assistant editor is retired and `/assistants` is now an explainer page that tells first-time visitors where their work went, host-gated so a legacy site answers appropriately.

- Assistant **records are untouched** — the Agent Designer reads and writes the same data. What disappeared is the old authoring surface (`assistants/assistant-form/`, `assistant-list`, `assistant-preview`), about 2,200 lines.
- `agents/migration/agents-migration.page.ts` and `shared/utils/legacy-migration-host.ts` — the explainer and its host gating.
- **The meaning of the `AGENTS_ENABLED` kill switch changed.** While both nouns shipped, turning it off degraded gracefully: the Agents nav disappeared and the Assistants editor was still there. There is nothing left to fall back to now — off means *no authoring surface at all*. Treat it as an outage switch, not a feature toggle. Records are untouched either way; the routes and pages are what disappear.

---

## 🐛 Bug fixes

- **Chat was completely down on any deploy carrying the `@`-mention cost work.** Every turn returned an AgentCore 424 because the container 500'd before the model was reached: `StreamCoordinator.stream_response()` forwarded `turn_agent_id` from inside its own body but never accepted it as a parameter. `ChatAgent` passes the kwarg on *every* turn, not just mention turns, so this was a total outage rather than a mention-path bug — reproduced on dev with an Agent attached and with no Agent and zero tools alike (#771).
- **A session's conversation forked when an `@`-mention ran.** The agent cache keys on *configuration*, so a mention builds a second `Agent`, each with its own `TurnBasedSessionManager`; both wrote the same DynamoDB session row and neither knew the other existed. The model answered "not in history" about messages the user could plainly see. Conversation history is now aliased across manager instances (#750).
- **Compaction state moved backwards for the same structural reason** — `initialize()` never re-runs on an agent-cache hit, so state loaded once went stale. It is now re-read every turn. A clobbered checkpoint is a prompt-cache *cost* bug before it is a correctness one (#761).
- **Timestamps were emitted as `2026-07-27T05:09:55.853557+00:00Z`** — an offset *and* a `Z`, which is not valid ISO 8601. `datetime.now(timezone.utc).isoformat()` already renders the offset; appending `"Z"` broke it. `new Date()` returns `Invalid Date`, and every SPA formatter fell back silently, so it read as missing data: the agent detail page showed "Last updated —" for an Agent edited minutes earlier, and admin Reports showed "recently" for every report ever filed. New `apis/shared/timestamps.py` plus SPA-side `iso-date.ts` (#772).
- **Every API-key request was denied every model.** `/chat/api-converse` built its `User` with a hardcoded `roles=["user"]` placeholder; no AppRole maps that JWT role, so permission resolution matched nothing and fell back to `default`, which grants no models in production — 403 "Access denied to model" regardless of the caller's real grants. An API-key record stores only key id, user id and name, so the owner's roles are now read back from the Users table per request, along with email and name — which also stops quota tier and cost attribution being charged against a synthetic `{user_id}@api-key` identity (#796).
- **~2,500 false "tool not found" warnings per day in production.** `ToolFilter` knew three tool classes (registry, gateway, external MCP) and warned on anything else. Context-bound tools are a fourth — they need request scope baked in at construction, so inference-api builds them per invocation and passes them as `extra_tools`, which `BaseAgent` appends *after* filtering. Every enabled one logged "not found in registry or catalog, skipping" and then worked fine (`create_artifact` 1,744, `analyze_spreadsheet` 1,690, `list_spreadsheets` 1,690 over 48h), drowning the one signal that branch exists to give: a genuinely stale tool id pinned in a saved session (#794).
- **Cache-TTL classification used the wrong clock.** `classify_cache_status` measured the gap to the immediately-previous call row, which is wrong the moment two prefixes interleave in one session — exactly what an `@`-mention does. The entry a call could have hit belongs to the last call with the *same* prefix. Measured on dev: a plain turn 266s after a mention was booked as `miss_avoidable` waste, but the entry it needed was written 308s earlier, past the 300s TTL — the re-write was unavoidable. Three of six rows in that session were false positives (#754).
- **The unopenable-Agent warning was unreadable where it mattered most.** It shipped inside the review card's identity column, competing for width with the agent name and the action buttons — measured at ~160px of a 528px card, wrapped across five lines. It now sits on its own full-width row beneath the decision, separated by a hairline rule. Structural only: no wording, logic or gating change, and Approve was never blocked on it. This is the signal standing between "approved" and a store tile that 404s for everyone but its author, so being on screen was not the same as being read (#776).
- **The agent detail page never loaded its agent** — broken since marketplace phase 3, now covered by a load-path spec (#740, #742, #743).
- Deleting an Agent left its entire `VERSION#` snapshot history in the table permanently — child rows live under the Agent's partition precisely so they never outlive what they concern (#792).
- Four version-snapshot gaps found in end-to-end testing, plus the version picker becoming unreachable after a rollback and the reviewer not being told why a diff was missing (#799, #801).
- The `limits` (degraded) runnability state is removed. It could never occur — it degraded only when a binding declared `config.optional == true`, a key read in exactly one place and written nowhere, with no API accepting it and no Designer control for it. Building toward it would also have contradicted the block-only binding model that `agent_binding_resolver` actually implements (#762).
- The admin roles list shows which roles carry delegated admin power (#807).
- A failed category change surfaces an error banner instead of failing silently (#795).
- Dialogs dismiss on a backdrop click across the SPA, and the agent feedback dialog scrolls instead of clipping (#811).

---

## 🔒 Security

- Backend security pins: `pillow` 12.2.0 → 12.3.0, new pins for `pyasn1` 0.6.4 and `soupsieve` 2.8.4 (#723).
- Frontend and infrastructure transitive vulnerabilities patched by lockfile regeneration. `aws-cdk-lib` was bumped rather than overridden, because it **bundles its dependencies** — an npm `overrides` entry cannot reach a CVE inside the bundle (#723).
- Docs site upgraded to Astro 7 to clear transitive advisories (#724).
- Uploaded Agent icons are always re-encoded, stripping EXIF (including GPS) before an icon is published institution-wide (#735).

---

## ⚠️ Breaking changes

| Change | Who is affected | What to do |
|---|---|---|
| Assistant editor retired; `/assistants` is now an explainer | Anyone linking directly to the old editor | Use the Agent Designer. Records are unchanged; `AGENTS_ENABLED=false` no longer falls back to the old editor — treat it as an outage switch |
| Publishing requires PUBLIC visibility | Authors submitting a private Agent | Go public from the submit dialog (now inline; previously required a detour to settings) |
| `limits` runnability state removed | API consumers reading `runnability` | Handle `ready` and `blocked` only |
| Gateway authorizer is immutable | Forks setting `CDK_GATEWAY_INBOUND_AUTH=jwt` on an existing Gateway | Do not. It fails mid-deploy. Create a new Gateway and re-register targets |
| A Gateway accepts one authorizer type | Forks with non-agent SigV4 Gateway callers | Migrate those callers before creating a JWT Gateway |

---

## 🏗️ Infrastructure

| Resource | Detail |
|---|---|
| `{prefix}-audit-log` DynamoDB table | New. `PK`/`SK`; `ActorIndex` + `RecentIndex` GSIs (ALL projection); PITR on; TTL `expiresAt`; AWS-managed encryption. Name at `/{prefix}/audit/audit-log-table-name` |
| `AgentDirectoryIndex` GSI | New, on `{prefix}-rag-assistants`. `GSI5_PK` = `LISTED#{category}`, `GSI5_SK` = `CREATED#{created_at}` (newest-first), ALL projection, **sparse** — only listed Agents carry the keys |
| `{prefix}-token-exchange-client` secret | New, **optional**. Created only when `tokenExchange` is configured; populated out of band |
| `AgentCoreGatewayConstruct` | Gains a `CUSTOM_JWT` inbound-authorizer path driven by `config.gateway.inboundAuth` |
| CDK config | New `agentMarketplace.enabled`, `gateway.inboundAuth`, optional `tokenExchange.url` / `tokenExchange.clientId` |

---

## 🔧 CI/CD

- `platform.yml` accepts three new repository variables: `CDK_GATEWAY_INBOUND_AUTH`, `CDK_TOKEN_EXCHANGE_URL`, `CDK_TOKEN_EXCHANGE_CLIENT_ID`. All three are safe to leave unset.
- `load-env.sh` threads them into CDK context and **validates the authorizer value** (`iam` | `jwt`), so a typo fails the deploy instead of silently falling through to the `iam` default.
- `test_non_admin_roles_get_403` no longer rebuilds the FastAPI admin app inside every Hypothesis example (~50ms × 100 across 130 routes against a 200ms deadline). It passed with no margin and failed as `DeadlineExceeded` under load, which reads as an auth regression rather than the timing artifact it was; adding any admin router made it likelier to trip (#808).
- SPA test isolation: cross-file state leaks that made `ng test` flaky are fixed in `src/test-setup.ts` (#759).

---

## 📦 Dependencies

| Component | Package | From | To |
|---|---|---|---|
| Backend | `pillow` | 12.2.0 | 12.3.0 |
| Backend | `pyasn1` | — | 0.6.4 (new pin) |
| Backend | `soupsieve` | — | 2.8.4 (new pin) |
| Infrastructure | `aws-cdk-lib` | 2.260.0 | 2.262.0 |
| Docs site | `astro` | 6.4.6 | 7.1.3 |
| Docs site | `@astrojs/starlight` | 0.39.3 | 0.41.4 |
| Docs site | `sharp` | 0.34.5 | 0.35.3 |

---

## 🚀 Deployment notes

**A CDK deploy is required.** Order: `platform.yml` → `backend.yml` → `frontend-deploy.yml`.

1. **Deploy `platform.yml` first and wait for the new GSI.** `AgentDirectoryIndex` on the assistants table backfills asynchronously — CloudFormation reports the stack green while the index is still `CREATING`, and the marketplace store queries it. Confirm `IndexStatus: ACTIVE` before shipping the backend:
   ```bash
   aws dynamodb describe-table --table-name {prefix}-rag-assistants \
     --query 'Table.GlobalSecondaryIndexes[?IndexName==`AgentDirectoryIndex`].IndexStatus'
   ```
   Note that `platform.yml`, `backend.yml` and `frontend-deploy.yml` share a concurrency group, so a merge cannot be relied on to order them — run and verify platform before the rest.
2. **Then `backend.yml`, then `frontend-deploy.yml`.**
3. **No data migration.** Existing Agents keep working; they simply have no listing and no version snapshots until an author submits one.
4. **Feature flags — all default ON, opt-out only. This is the GA moment.** An environment with no variable set gets the Agent store live for every authenticated user on this deploy, with an empty catalog until the first Agent is approved. If an environment should *not* get the store yet, set the variable **before** deploying — the flags are empty-string-safe, so an unset or blank GitHub Actions variable resolves to *enabled*, never to off.
   - `AGENT_MARKETPLACE_ENABLED=false` (or `CDK_AGENT_MARKETPLACE_ENABLED=false`) disables the marketplace surface on app-api.
   - `AGENTS_ENABLED=false` now removes the *only* authoring surface — see the rename section above before using it.
   - Agent categories **self-seed on first read** (`categories.ensure_seeded`), so there is no per-environment seeding step and nothing to forget.
5. **Gateway inbound auth — leave `CDK_GATEWAY_INBOUND_AUTH` unset** on every environment whose Gateway already exists. Setting it to `jwt` against a live `AWS_IAM` Gateway fails mid-deploy and cannot be fixed by a config change; it applies only to a newly created Gateway.
6. **Token exchange is dormant by default.** Leave `CDK_TOKEN_EXCHANGE_URL` and `CDK_TOKEN_EXCHANGE_CLIENT_ID` unset unless the organization runs a token service with a token-exchange endpoint. If enabling: deploy, then populate the `{prefix}-token-exchange-client` secret out of band with the `client_id → client_secret` map.
7. **Delegated admin scopes require no action.** Existing `system_admin` roles are unaffected; no role gains or loses access on deploy. Delegation is opt-in per role from the roles admin page.
8. **The audit log starts empty** and fills from the first role mutation after deploy. It is not backfilled — there is no historical source to backfill from.

---

# Release Notes — v1.11.1

**Release Date:** July 24, 2026
**Previous Release:** v1.11.0 (July 24, 2026)

---

> 🚀 **No CDK deploy required this release** — backend-only, no new AWS resources, no dependency changes. Ship the artifact-render Lambda via `backend.yml`. No per-environment action and no data migration: existing Markdown artifacts are corrected at download time.

---

## Highlights

v1.11.1 is a focused patch fixing **Markdown artifact downloads**. Saving a Markdown artifact previously produced a `.html` file containing the render scaffolding instead of the `.md` source the user authored. Downloads now recover the original Markdown and save it as a proper `.md` file. Preview-panel rendering is unchanged, and the fix applies to artifacts created before this release with no re-storage.

## 🐛 Bug fix

- **Markdown artifacts downloaded as HTML wrapper instead of `.md` source.** Markdown artifact records keep `content_type=text/markdown`, but S3 holds the writer's HTML render wrapper — the raw Markdown base64-embedded in a `<script id="md-src">` block. The download path served those wrapper bytes verbatim and mapped `text/markdown` → `html`, so "save" yielded a `.html` file of the render scaffolding rather than the authored document. On download (`?download=1`) of a Markdown record, `backend/src/lambdas/artifact_render/handler.py` now recovers the embedded raw Markdown and serves it as `text/markdown` with a `.md` extension. Because the source is already embedded in stored artifacts, this works for existing records with no re-storage. If the embed marker is ever absent (older render or template drift), the handler falls back to the wrapper bytes as `.html` so the download never fails. Rendering in the preview panel is untouched (#726)

## 🚀 Deployment notes

- **No CDK deploy needed** — no new AWS resources, no infrastructure changes, no dependency changes. Deploy the artifact-render image via `backend.yml`; the fix is entirely within the artifact-render Lambda.
- **No per-environment action** — no catalog seeding, RBAC grants, or feature flags. Existing Markdown artifacts are corrected on download automatically.

---

> 🚀 **No CDK deploy required this release** — no new AWS resources, no dependency changes, no infrastructure edits. Ship backend code via `backend.yml` and the SPA via `frontend-deploy.yml`. **Per-environment action:** two new tool catalog entries ("PowerPoint Presentations", "File Workspace") must be seeded and granted via RBAC before users can enable them — see Deployment notes.

---

## Highlights

v1.11.0 adds **two new agent capabilities to chat**. First, a **PowerPoint presentation toolset** — the agent can create, edit, read, and list real `.pptx` decks, built with python-pptx inside the sandboxed Code Interpreter and delivered through the chat Files panel with a download link — completing the office-document trio alongside the existing Excel and Word tools. Second, a generic **File Workspace toolset** that gives the agent a first-class way to list, read, and save text files (Markdown, CSV, JSON) in a conversation's workspace, over the same user-files store. Both ship as single catalog toggles, off by default. The release also fixes two rendering bugs: Markdown artifacts that came out as raw `#`/`**` source when authored under the default HTML type, and empty "Thinking" collapsibles that appeared on conversation reload for signature-only reasoning blocks.

## PowerPoint presentations in chat

Users can ask the agent to build or revise real PowerPoint decks mid-conversation — slide outlines, briefing decks, templated layouts — and get a downloadable `.pptx` back in the chat's Files panel. The capability mirrors the Excel and Word toolsets: one admin toggle provisions the whole round-trip.

### Backend

- `agents/builtin_tools/powerpoint_presentation_tool.py` (750+ lines) — five tool factories: `make_create_powerpoint_presentation_tool`, `make_modify_powerpoint_presentation_tool`, `make_list_powerpoint_presentations_tool`, `make_read_powerpoint_presentation_tool`, and `make_list_powerpoint_layouts_tool`. Generation and edits run python-pptx inside the sandboxed AgentCore Code Interpreter; nothing executes in the API container, and identity is captured by closure (same pattern as the Word/Excel tools, since the runtime does not populate `ToolContext`).
- `apis/inference_api/chat/routes.py` — `_build_powerpoint_presentation_tools` injects the toolset at runtime when the catalog toggle is enabled. One catalog entry ("PowerPoint Presentations", gate key `create_powerpoint_presentation`, `enabledByDefault: false`) provisions all five tools.
- Generated files persist to the user-files bucket (`S3_USER_FILES_BUCKET_NAME`) and surface in the session's Files panel.

### Frontend

- The generic `file-download-renderer` component (introduced in v1.10.0 for Word/Excel) now also handles `.pptx`, so generated presentations render through the same inline download card.

## File Workspace toolset

Gives the agent a durable, generic file surface over a conversation's workspace — distinct from the format-specific office tools. The model can enumerate what files exist, read uploaded text files on demand instead of front-loading them into context, and save text deliverables back to the conversation.

### Backend

- `agents/builtin_tools/workspace_tools.py` — three tools: `workspace_list`, `workspace_read`, `workspace_write`. Reads uploaded text files on demand and writes text deliverables (Markdown, CSV, JSON) to the user-files store, where they appear in the chat Files panel with a download link.
- `apis/shared/files/workspace.py` (430+ lines) — new shared module implementing the workspace read/write surface over the user-files store.
- `apis/shared/feature_flags.py` — `workspace_tools_enabled()` gates the feature per environment via `WORKSPACE_TOOLS_ENABLED` (**default ON, kill switch** — only the literal `false` disables). This is independent of the `workspace_files` catalog entry, which governs *who* may use the tools via RBAC.
- `apis/shared/files/models.py` — file records gain a display-only `source` provenance field ("upload" or the id of the tool that produced the file); never part of an access decision.
- `apis/inference_api/chat/routes.py` — `_build_workspace_tools` injects the set when the catalog toggle is enabled. One catalog entry ("File Workspace", gate key `workspace_files`, `enabledByDefault: false`).

### Test Coverage

430+ lines of new tests across `tests/agents/builtin_tools/test_workspace_tools.py` and `tests/shared/test_workspace.py` covering the tool surface and the workspace store. Design captured in `docs/specs/session-workspace-tools.md`.

## 🐛 Bug fixes

- **Markdown artifacts rendered as raw source.** `create_artifact`'s `content_type` defaults to `text/html`, so a request like "make a markdown recipe" easily produced raw Markdown stored under the HTML type — which then rendered as run-together `#`/`**` source instead of a formatted document. The service now reclassifies HTML-typed content that lacks a full HTML document shell (`<!doctype html>` / `<html>`) as Markdown, which the writer wraps into a proper render document; the tool guidance also now steers prose deliverables (reports, articles, notes, recipes) to Markdown mode. Non-HTML and genuine HTML-document content pass through untouched (#720)
- **Empty "Thinking" blocks appeared on reload.** Some models (e.g. Sonnet 5) persist a signature-only `reasoningContent` block — empty `reasoningText.text`, no redacted content — which Bedrock requires kept in the message for follow-up calls. The live stream parser already guarded on this, but the history-rehydration path bypassed it and painted an empty "Thinking" collapsible on reload. A `hasRenderableReasoning()` guard now mirrors the component's own visibility logic and the parser's guard, so the block is only painted when it has reasoning text or redacted content. Display-only: the block is left untouched in the persisted message to preserve the signature/prompt-cache contract (#721)

## 🚀 Deployment notes

- **No CDK deploy needed** — no new AWS resources, no infrastructure changes, no dependency changes (python-pptx runs in the sandboxed Code Interpreter, like openpyxl for Excel). Run `backend.yml` (app-api / inference-api) and `frontend-deploy.yml` as usual.
- **Seed and grant the two new tool catalog entries per environment** — "PowerPoint Presentations" (gate key `create_powerpoint_presentation`) and "File Workspace" (gate key `workspace_files`) ship in the bootstrap seed data with `enabledByDefault: false`. Environments seeded before this release won't have the rows: add them via the admin Tools page (or re-run the tools seeding) and grant them to the appropriate roles via RBAC.
- **File Workspace kill switch** — `WORKSPACE_TOOLS_ENABLED` defaults ON; no configuration is required to enable the feature. Set it to `false` on the inference-api environment to disable the workspace tools entirely for an environment, independent of the catalog grant.

---

# Release Notes — v1.10.0

**Release Date:** July 21, 2026
**Previous Release:** v1.9.0 (July 20, 2026)

---

> 🏗️ **CDK deploy required this release** — the SPA CloudFront distribution's response-headers policy changes (MCP Apps CSP fix). No new AWS resources, no data migration, no dependency changes. Standard order: `platform.yml` → `backend.yml` → `frontend-deploy.yml`.

---

## Highlights

v1.10.0 brings **Excel spreadsheets to chat**: the agent can now create, edit, read, and list real `.xlsx` workbooks — built with openpyxl inside the sandboxed Code Interpreter and delivered through the chat Files panel with a download link — governed by a single "Excel Spreadsheets" catalog toggle. The same work extracts a shared office-document storage module that the Word tools now ride on. The release also fixes a day-one CSP gap that **blocked every MCP App iframe on deployed environments**: the SPA's `frame-src` never allowed the `mcp-sandbox` origin, and localhost testing (which bypasses CloudFront's headers) had masked it since the feature shipped.

## Excel spreadsheets in chat

Users can ask the agent to build or revise real Excel workbooks mid-conversation — budget templates, rosters, data exports — and get a downloadable `.xlsx` back in the chat's Files panel.

### Backend

- `agents/builtin_tools/excel_spreadsheet_tool.py` (530+ lines) — four tools: `create_excel_spreadsheet`, `modify_excel_spreadsheet`, `list_excel_spreadsheets`, `read_excel_spreadsheet`. Generation and edits run openpyxl inside the sandboxed AgentCore Code Interpreter; nothing executes in the API container.
- `agents/builtin_tools/office/_storage.py` — new shared storage module (Code Interpreter execution + S3 persistence) common to Word and Excel; `word_document_tool.py` is refactored onto it, dropping ~270 lines of duplicated plumbing.
- `apis/inference_api/chat/routes.py` — the toolset is injected at runtime when the catalog toggle is enabled. One catalog entry ("Excel Spreadsheets", gate key `create_excel_spreadsheet`, `enabledByDefault: false`) provisions all four tools; it is distinct from the spreadsheet *analysis* tools (`list_spreadsheets`/`analyze_spreadsheet`), which read uploaded tabular files.
- Generated files persist to the user-files bucket (`S3_USER_FILES_BUCKET_NAME`) and surface in the session's Files panel.

### Frontend

- New generic `file-download-renderer` component replaces the Word-specific `word-document-renderer` — all generated office documents (Word and Excel) now share one inline card with filename, type, and download link.

## 🐛 Bug fixes

- **MCP App UIs were blank on every deployed environment** — demoing an MCP App (e.g. Excalidraw) on a domained deploy failed with `Framing 'https://mcp-sandbox.{domain}/' violates the Content Security Policy directive: "frame-src 'self' https://artifacts.{domain}"`. Root cause: the MCP Apps rollout wired the *inbound* direction (the sandbox proxy's `frame-ancestors` is locked to the SPA origin) but never extended the SPA's own *outbound* `frame-src`, and all live verification ran on localhost:4200, which bypasses CloudFront's response headers. `PlatformStack` now threads the sandbox proxy origin (`https://mcp-sandbox.{domain}`) into `SpaDistributionConstruct` as a required prop, and a new synth-time test (`infrastructure/test/spa-frame-src-csp.test.ts`) asserts both iframe origins are present in the frontend headers policy so the gap can't silently reopen (#714)

## 🚀 Deployment notes

- **Run `platform.yml`** — the SPA distribution's `ResponseHeadersPolicy` changes (CSP `frame-src` gains the `mcp-sandbox.{domain}` origin). Quick, low-risk CloudFront-only update; then `backend.yml` and `frontend-deploy.yml` as usual.
- **Enable the Excel tool per environment** — the "Excel Spreadsheets" catalog entry ships in the bootstrap seed data with `enabledByDefault: false`. Environments seeded before this release won't have the row: add it via the admin Tools page (or re-run the tools seeding) and grant it to the appropriate roles via RBAC.
- The MCP Apps fix needs no configuration — environments where `mcp-sandbox.{domain}` is deployed start working as soon as the new headers policy is live (a hard refresh may be needed to drop the cached CSP).

---

# Release Notes — v1.9.0

**Release Date:** July 20, 2026
**Previous Release:** v1.8.0 (July 19, 2026)

---

> 🏗️ **CDK deploy required this release** — a new CloudWatch dashboard construct and one new env var on the inference-api Runtime. No data migration, no backfills, no dependency changes. Standard order: `platform.yml` → `backend.yml` → `frontend-deploy.yml`.

---

## Highlights

v1.9.0 makes **Bedrock prompt-cache spend stable and measurable**. A production conversation audit found 75% of one session's cost was avoidable cache re-writes; this release fixes the three defects behind that waste — restored history that mutated every turn, skills injected in nondeterministic order, and a single fragile cachePoint — and adds the observability to prove it and catch regressions: every model call now records prefix fingerprints and a `cacheStatus` verdict, admins get a per-session **Cost Anatomy** drill-down page, and operators get a CloudWatch dashboard with alarms on avoidable waste. Smaller fixes: the chat input textarea scrolls and resets correctly, and Word-document saves work on the deployed runtime (the container wasn't told which S3 bucket to use).

## Prompt-cache stability — stop paying for avoidable re-writes

Bedrock prompt caching is exact-prefix-match: if any byte of the cached prefix changes between turns, the whole prefix re-writes at the $2.50/MTok cache-write premium instead of reading at the ~$0.30/MTok cache-read rate. On a typical 35k–150k-token session prefix, one silent cache-buster costs more per turn than most turns' actual work. Three were found and fixed (#697):

- **Restored history is now byte-stable.** Tool-content truncation ran on every session restore behind a sliding protected-turns window, so each new turn re-mutated the turn that had just aged past the window — invalidating the cache nearly every turn. Truncation is now driven by a persisted `truncation_anchor` in the session's compaction state: it advances only when the compaction checkpoint advances (where the slice already pays a single re-write) or opportunistically when the cache TTL (default 300s, `AGENTCORE_MEMORY_COMPACTION_CACHE_TTL_SECONDS`) has lapsed since the previous turn and the cache entry is dead anyway — so pending truncations apply for free.
- **Skills inject in deterministic order.** Skill records reached the `<available_skills>` system-prompt block in whatever order DynamoDB `batch_get_item` and Python set iteration produced, changing the system prompt between turns of the same session. Ordering is now sorted at three layers: the skills repository, the RBAC grant-union resolver (which returned `list(set)` — order varies per process via hash randomization), and the injection point itself.
- **Three cachePoints instead of one.** The auto strategy places a single message-level cachePoint; when its lookup misses (one proven mode: a wide parallel tool fan-out pushes the previous checkpoint past Anthropic's ~20-block cache lookback), *nothing* was read and the entire prefix re-wrote. Requests now carry 3 of Bedrock's max-4 cachePoints — toolConfig tail, system-prompt tail, and the existing last-user-message point — so a message-level miss still reads the stable tools+system prefix from cache. The added points are gated on `ModelConfig.bedrock_cache_points_supported()` since non-Anthropic models reject them.

## Prompt-cache observability — every model call explains its cache behavior

Diagnosing the waste above originally took hours of manual forensics against raw DynamoDB cost rows. That whole class of investigation is now a column diff (#697, #700, #699, #701).

### Backend

- `PrefixFingerprintHook` (a Strands `BeforeModelCallEvent` hook) hashes the three cacheable prefix components per model call — toolConfig (order-sensitive canonical JSON), the effective system prompt captured *after* AgentSkills injection, and message history excluding the newest message — and the stream coordinator persists them on the turn's cost rows. When a cache miss happens, the hash that changed between consecutive calls names the cache-buster.
- Each cost row gets a write-time `cacheStatus` — `first_write`, `hit`, `miss_ttl_expired`, `miss_avoidable`, or `uncached` — derived against the session's previous cost row, plus `wastedUsd` for avoidable misses priced at the cache-write premium over cache-read from the row's own pricing snapshot. Turn rows now write sequentially so each call classifies against its true predecessor. A follow-up fix (#701) classifies the first write after a run of below-threshold calls as `first_write` rather than `miss_avoidable`, so short-prompt sessions don't inflate the waste metrics.
- Session rows carry rollups next to `totalCost` — `totalCacheReadTokens`, `totalCacheWriteTokens`, `avoidableMissCount`, `wastedUsd` — so lists and admin views get a cache-efficiency ratio without scanning cost rows.
- `GET /admin/costs/sessions/{sessionId}/calls` (admin-only) returns the chronological per-call rows with token splits, cost, `cacheStatus`, and fingerprints, plus a session-level cache summary.
- Everything derived is behind `PROMPT_CACHE_OBSERVABILITY_ENABLED` (default ON, `=false` to disable the hook, the classification's extra GSI read, and EMF emission). Raw cache read/write token rollups are unaffected — they're usage passthrough, not derived.

### Frontend

- New **Session Cost Anatomy** page at `/admin/costs/sessions/:id`: summary tiles (total cost, cache efficiency, avoidable misses, wasted USD, cache read/write tokens) over a chronological calls table with color-coded `cacheStatus` badges. Fingerprint diffing flags which hash — tools, system, or history — flipped versus the previous fingerprinted call, which is the diagnosis on any `miss_avoidable` row. Expandable rows show full hashes and message counts; a session-id lookup form on the Cost Analytics dashboard is the entry point.

### Infrastructure

- `PromptCacheObservabilityConstruct` (new `lib/constructs/observability/` area, composed into `PlatformStack`) builds a CloudWatch dashboard over the `AgentCoreStack/PromptCache` EMF namespace both APIs emit into: cache read/write token trends, a cache-efficiency MathExpression, AvoidableMiss and WastedUsd, and a Logs Insights widget grouped by `cacheStatus`. Console-only alarms on AvoidableMiss and WastedUsd Sums (stricter thresholds in prod, `NOT_BREACHING` on missing data so the kill switch keeps them quiet). Deliberately no SNS — alerting infra remains out of scope, matching kb-sync and scheduled runs.

### Test Coverage

~1,800 lines of new tests: fingerprint/classification unit tests (including the below-threshold regression), cachePoint position and budget assertions, forced-order skill-sorting regressions, CDK construct assertions, and Vitest specs for the anatomy page, diff util, and HTTP service.

## 🐛 Bug fixes

- **The chat input became unusable on long prompts.** The textarea carried `overflow-hidden` with unclamped height growth, so past its 200px cap the content could neither be seen nor scrolled — and after sending, the box stayed expanded. Growth is now clamped with scrolling enabled past the cap, and the input resets to its base height on submit (#696)
- **Word-document saves failed on the deployed runtime with `AccessDenied`.** The AgentCore Runtime env set the user-files *table* name but not `S3_USER_FILES_BUCKET_NAME`, so the Word tools fell back to a literal `user-files` bucket the role has no access to. The env var is now set on the Runtime (#702), and the tools fail fast with a clear "storage is not configured" message — before spending a Code Interpreter run — if the variable is ever missing again (#706)

## 🏗️ Infrastructure

- New CloudWatch dashboard + alarms construct (see spotlight above) — CloudWatch-console resources only, no SNS, no new IAM of note.
- `S3_USER_FILES_BUCKET_NAME` on the inference-api Runtime environment; the role's existing `UserFilesBucketAccess` grant already covers the bucket (#702)
- New env var `PROMPT_CACHE_OBSERVABILITY_ENABLED` on app-api and inference-api (default ON; set `=false` per environment to disable the observability layer — caching itself stays on).

## 🚀 Deployment notes

Standard order, and this release uses all three: **`platform.yml` first** (the dashboard construct and the Runtime env var are CDK changes; the runtime picks up its current image via SSM, so the infra deploy is safe on its own) → `backend.yml` → `frontend-deploy.yml`. No backfills, no data migration, no dependency changes.

After deploy, the **PromptCache dashboard** appears in the CloudWatch console. Expect `first_write` rows at the start of sessions and after idle gaps — only `miss_avoidable` indicates regression. The observability layer is per-call metadata; if it ever needs to be silenced in an environment, set `PROMPT_CACHE_OBSERVABILITY_ENABLED=false` and redeploy that service — the alarms go quiet on missing data by design.

---

# Release Notes — v1.8.0

**Release Date:** July 19, 2026
**Previous Release:** v1.7.1 (July 17, 2026)

---

> ⚠️ **Three backfill scripts must be run per environment.** Skills activate on the `backend.yml` deploy itself — before any CDK deploy — because deployed containers set no `SKILLS_ENABLED` and unset now reads as enabled. No new AWS resources. See the Deployment notes at the end of this entry.

---

## Highlights

v1.8.0 delivers **Skills v2**, a ground-up redesign of what a skill *is*. Skills were containers that bound tools; they are now **pure, portable knowledge bundles** — instructions plus reference files — that an Agent loads on demand through progressive disclosure. For the first time, **any signed-in user can author their own skills**, not just admins, and a skill bound to a shared Agent resolves for whoever uses that Agent. Skills ship **enabled by default**. This release also completes the session-metadata static-sort-key migration (issue #175) through its backfill and read-contraction phases, and collapses the two artifact tool-catalog rows into a single "Artifacts" toggle. Operators must run three backfill scripts; there are no new AWS resources and no dependency changes.

## Skills v2 — knowledge bundles as an Agent primitive

A skill used to be a container that carried its own bound tools, which made it a second, parallel permission axis fighting the RBAC one. A skill is now **instructions + reference files only**. `allowedTools` still persists on the record but is advisory metadata that **never grants a tool** — tool access is RBAC's job, exclusively. The agent discovers skills by name and description (L1), reads `SKILL.md` when one looks relevant (L2), and pulls individual reference files through the new `read_skill_file` tool only as needed (L3), so a large skill library costs almost nothing in context until something is actually used.

The storage format is now **agentskills.io-standard bundles** in S3, with a `SKILL.md` write-through projection generated from the DynamoDB row. That makes a skill prefix portable: it can be handed to a managed Harness as `{"s3": {"uri": ...}}` or exported as-is, which the previous content-addressed layout could not do.

### Backend

- `apis/app_api/skills/routes.py` — user-facing surfaces: the picker (`GET /skills/`, `PUT /skills/preferences`) and owner-scoped My Skills CRUD (`/skills/mine/*`), including bundle file upload. Ownership resolves through `UserSkillService`, so a skill you do not own is indistinguishable from one that does not exist.
- The runtime swapped the bespoke `SkillAgent` for the Strands **`AgentSkills` plugin**; `agent_type="skill"` remains a temporary ChatAgent alias for pre-existing snapshots and is scheduled for removal one release out.
- `read_skill_file` added for L3 progressive disclosure. `scripts/` files in a bundle are accept-and-inert by design — stored, listed, and readable, never executed.
- A new user tier in the app-roles table, GSI4-indexed for owner lookups.
- The `skills` RBAC capability gate was **removed**. It kept the surfaces admin-only during rollout, but it could not be granted from the admin roles UI at all — that form builds `grantedTools` from the tool catalog, and a capability id is not a tool. An admin granting a catalog skill to a role would have found it silently invisible to that role's users with no way to fix it in-product. Access is now `SKILLS_ENABLED` per environment plus a role's `grantedSkills` per cohort, which the roles UI *can* edit.

### Frontend

- A **Skills** section in the chat model-settings panel lists every skill the user can reach — RBAC-granted catalog skills **union** the ones they authored — each with an opt-in toggle. Skills are off by default; the section renders only when the user actually has one, so it stays invisible for users with no grants.
- When an Agent binds a fixed skill set, the picker shows only those skills, locked, with an explanatory note.
- A `/my-skills` page and authoring form for the user tier. **The sidenav entry is deliberately hidden** pending a decision on how users should reach the page — the route, page, and backend surface are fully live and reachable by direct URL.

### Infrastructure

- `SKILLS_ENABLED` threaded into app-api and inference-api from `config.skills.enabled`, default ON with a `CDK_SKILLS_ENABLED=false` per-environment kill switch. Design-time refuses to bind a skill while the flag is off on app-api, so the two services must stay in step — a mismatch would let an Agent be built with skills the runtime then blocks.
- **No new AWS resources.** The skill-resources S3 bucket and the app-roles table both predate this release.

### Test Coverage

~1,800 lines of new skills tests across the runtime plugin, the user tier's ownership boundaries, resource-store layout, and the SPA picker and My Skills services.

## Session-metadata migration completed (issue #175, Phases 2–3)

v1.7.0 landed the read side and v1.7.1 the write side; v1.8.0 finishes the job for rows that never get written again. **Phase 2** adds `backend/scripts/backfill_session_static_sk.py`, which migrates the cold tail of legacy rows and deletes ghost stubs. It is dry-run by default, idempotent, and throttled; the static put is guarded by `ConditionExpression=Attr("SK").not_exists()` so it can never clobber a row a live writer already migrated with fresher data. Its `--set-marker` flag re-scans and **refuses to write the completion marker while any legacy rows remain**, so multiple passes are expected on a large table.

**Phase 3** then lets session listing skip the legacy base-table query entirely once that marker (`PK=MIGRATION#session-sk`) is present — one DynamoDB query per list call instead of two. It **fails open**: an absent marker, or a handled GSI error, keeps the dual-read path, so the deploy is order-independent and downstream forks that never run the backfill are unaffected. The marker result is memoised only on success, so a container that started before the backfill picks up the flip on a later list call with no restart.

Users notice nothing — list contents, ordering, and cursor encoding are unchanged.

## Artifacts consolidated to one catalog toggle

Admins previously saw "Create Artifact" and "Update Artifact" as two unrelated rows in the tool catalog, to grant and toggle separately, even though updating an artifact is meaningless without creating one. They are now a single **"Artifacts"** entry (`toolId` `create_artifact`) that injects both tools at runtime.

This requires `backend/scripts/backfill_artifact_tool_merge.py`, which promotes existing role grants, user tool preferences, and assistant bindings from the retired `update_artifact` id onto the kept one before deleting the retired row. Promotion happens **before** deletion, so an aborted run degrades to "both granted" rather than "neither." An explicit *enable* of the retired id carries over; an explicit *disable* deliberately does not.

## 🐛 Bug fixes

- **Saving an Agent with an invalid form looked like a click that did nothing.** The inline validation error was usually below the fold once the author had scrolled to the Model or Skills sections, so the save button appeared inert. The first invalid control is now scrolled into view and focused, with a "Fix the highlighted fields before saving" toast. The same change corrects stale copy on the agent and admin skill forms that still described skills as carrying their own bound tools.

## ⚠️ Breaking changes

- **Skills no longer bind tools.** This is a conceptual break, not a data break: existing skill rows keep working and keep their `allowedTools` values, but those values stop granting anything. Any workflow that relied on a skill to confer tool access must grant those tools through RBAC instead. The skills mode toggle is gone.
- **The `update_artifact` tool-catalog row is retired.** Run the artifact backfill below before or immediately after deploying, or roles and users that had been granted `update_artifact` alone will lose it.

## 🏗️ Infrastructure

- No new AWS resources, no new IAM, no dependency changes.
- The only CDK delta is the `SKILLS_ENABLED` environment variable on the app-api task definition and the inference-api Runtime. Because unset now reads as enabled, **a CDK deploy is not required to activate skills** — it only makes the setting explicit, and is required only to *disable* skills in a given environment.

## 🚀 Deployment notes

Standard order: `platform.yml` (optional this release — see above) → `backend.yml` → `frontend-deploy.yml`.

**Skills go live the moment `backend.yml` completes.** With the surfaces ungated, any signed-in user can then author skills at `/my-skills` and use any skill their role grants. Nothing is *linked* — the sidenav entry is hidden and no catalog skills are granted by default — but the route is present in the SPA bundle. If an environment is not ready for that, set `CDK_SKILLS_ENABLED=false` for it and deploy `platform.yml` **before** `backend.yml`.

Then run the three backfills, each **dry-run first** (all three are dry-run by default and idempotent), dev before prod:

```bash
# 1. Artifact tool merge — required; the retired row is deleted at the end
python backend/scripts/backfill_artifact_tool_merge.py --table <prefix>-app-roles
python backend/scripts/backfill_artifact_tool_merge.py --table <prefix>-app-roles --apply

# 2. Skill bundles — brings any pre-v2 skill up to the agentskills.io layout
python backend/scripts/backfill_skill_bundles.py \
    --table <prefix>-app-roles --bucket <prefix>-skill-resources
python backend/scripts/backfill_skill_bundles.py \
    --table <prefix>-app-roles --bucket <prefix>-skill-resources --apply

# 3. Session static-SK cold tail — optional but unlocks the Phase 3 read path
python backend/scripts/backfill_session_static_sk.py --table <prefix>-sessions-metadata
python backend/scripts/backfill_session_static_sk.py \
    --table <prefix>-sessions-metadata --apply --set-marker
```

Notes on each: the artifact script promotes grants before deleting the retired row, so an interrupted run is safe. The skill-bundle script *copies* legacy objects and only removes them with an explicit `--delete-legacy` once the copy is verified. The session script deletes ghost stubs and migrated legacy rows permanently and has no rollback path — take the dry-run output seriously, and expect `--set-marker` to no-op until a pass reaches zero legacy rows. Session listing keeps working correctly whether or not you ever run script 3.

---

# Release Notes — v1.7.1

**Release Date:** July 17, 2026
**Previous Release:** v1.7.0 (July 17, 2026)

---

> 🚀 **Backend-only release.** No CDK deploy and no data migration — ship through `backend.yml`. Session rows self-migrate to the static sort key on their next write; nothing to run.

---

## Highlights

v1.7.1 is a patch fixing a Word-document save failure and advancing the **session-metadata static-sort-key migration** (issue #175) to its write side. Saving a generated Word document no longer fails with `PermanentRedirect` in the AgentCore Runtime — the S3 client now resolves the user-files bucket's real region instead of trusting `AWS_REGION`. On the storage side, new sessions are now **born with a static sort key** and legacy rows **self-migrate in place** on their next write, so rows stop rotating on every message — structurally eliminating the ghost-row race behind the "Failed to parse session item" warnings and closing the first-turn duplicate-row race.

## Fixed — Word-document saves failing with `PermanentRedirect`

The user-files S3 client pinned its endpoint to `https://s3.{AWS_REGION}.amazonaws.com`. In the AgentCore Runtime, `AWS_REGION` does not reliably match the bucket's region, and the explicit `endpoint_url` disabled botocore's automatic S3 region redirect — so `PutObject` failed with `PermanentRedirect` and Word-document saves broke. The client (`agents/builtin_tools/word_document_tool.py`) now resolves the bucket's true region via `HeadBucket` (reading the `x-amz-bucket-region` header, which maps to the `s3:ListBucket` permission the runtime role already holds — avoiding the ungranted `s3:GetBucketLocation`) and drops the hardcoded `endpoint_url`. This fixes both the save and the presigned download URL region; if the region lookup is ever unavailable, botocore's now-enabled built-in redirect still corrects it.

## Session-metadata static-sort-key migration (issue #175, write-side)

v1.7.0 landed the read side (every reader tolerates both sort-key schemes); v1.7.1 turns on the **write** side. New sessions are now created at a static base sort key (`S#{session_id}` plus the `SessionRecencyIndex` keys) behind a real `attribute_not_exists(PK)` conditional put, and any still-legacy row does a one-time in-place migration to the static SK on its next write. Because the row no longer encodes `lastMessageAt` in the sort key, it never moves — the ghost-row race that produced "Failed to parse session item" warnings is structurally eliminated for every migrated row, and the deterministic sort key makes the first-turn duplicate-row guard meaningful for the first time. `delete_session` now resolves the raw sort key via the GSI (catching migrated rows the old `S#ACTIVE#…` reconstruction missed) and soft-deletes in place; the sparse recency index is set for active rows and removed for deleted ones. All resolve-then-update writers already operate on the current sort key and need no change. Covered by `TestWriteSideMigration` (born-static, one-time migrate, no rotation, in-place/legacy soft-delete, end-to-end) against the real `ConditionalCheckFailedException` contract.

## 🚀 Deployment notes

Ship through `backend.yml` (app-api + inference-api). No CDK deploy and no data migration — rows migrate themselves on their next write, and readers already tolerate both schemes as of v1.7.0. No breaking changes.

---

# Release Notes — v1.7.0

**Release Date:** July 17, 2026
**Previous Release:** v1.6.1 (July 16, 2026)

---

> 🏗️ **Platform (CDK) deploy required.** This release adds a new `SessionRecencyIndex` GSI on the sessions-metadata table, so it ships through `platform.yml` (CDK) **before** `backend.yml`. Adding the index is a no-op until rows populate its keys and the backend degrades gracefully if it's missing, so deploy order is not load-bearing — but the GSI must exist before the static-sort-key migration proceeds past this release. No data migration and no breaking changes.

---

## Highlights

v1.7.0 gives the agent a full **Word (.docx) document toolset** and advances the **session-metadata static-sort-key migration** (issue #175) through its read-side phases. The agent can now **create, modify, list, and read Word documents** — each backed by `python-docx` running in a Bedrock Code Interpreter sandbox and persisted to the same user-files store as every other generated file — and the result renders inline in chat with a download button. The whole toolset sits behind the single `create_word_document` capability toggle. On the storage side, a new sparse `SessionRecencyIndex` GSI and a **dual-scheme union reader** let session listing work whether or not a session's base sort key has been migrated yet, so the migration can roll out safely in any deploy order. This release also bumps `strands-agents` to 1.48.0 to fix an "Agent force-stopped" crash that hit any turn attaching a non-PDF document. Operators run a CDK deploy for the new GSI; everything else ships through the backend and frontend pipelines.

## Word document toolset

The agent can now produce and edit real Word documents. Four tools — `create_word_document`, `modify_word_document`, `list_word_documents`, `read_word_document` — run `python-docx` inside a Bedrock Code Interpreter session and write to the existing user-files store (S3 + DynamoDB), so generated `.docx` files are persisted and delivered exactly like every other user file. The finished document renders inline in the chat transcript with an accessible download button, no separate export step. The entire toolset is provisioned per-request behind one capability toggle (`create_word_document`), so admins enable Word support with a single grant.

### Backend

- `agents/builtin_tools/word_document_tool.py` — the create/modify/list/read toolset (~730 lines), each tool executing `python-docx` in a Code Interpreter session and round-tripping through the user-files store.
- `apis/inference_api/chat/routes.py` — `_build_word_document_tools` injects the toolset per request when the `create_word_document` capability is enabled.
- `scripts/seed_bootstrap_data.py` — seeds `create_word_document` into `DEFAULT_TOOLS` as "Word Documents" (with updated seed tests).

### Frontend

- `renderers/word-document-renderer.component.ts` — a new `word_document` inline-visual renderer showing the generated file with an accessible download button, styled with Tailwind utilities (no scoped CSS); wired into `inline-visual.component.ts`.

### Related fix

- `TurnBasedSessionManager` gains a restore-time content-block sanitizer that drops empty/typeless blocks from restored history, which had caused Bedrock ConverseStream `messages.N.content.M.type: Field required` errors on resume.

## Session-metadata static-sort-key migration (issue #175, read-side)

Active-session listing is being migrated to a **static** base sort key (`S#{session_id}`) with recency served by a dedicated index, replacing a scheme that encoded `lastMessageAt` into the sort key and rotated rows on every message (the source of ghost rows and duplicate-row races). This release lands the read side so every reader tolerates both schemes before any write starts self-migrating rows.

### Infrastructure — Phase 0

- `data/cost-tracking-tables-construct.ts` — new sparse `SessionRecencyIndex` GSI (`GSI4_PK=USER#{id}`, `GSI4_SK={lastMessageAt}#{session_id}`, projection ALL) for newest-first active-session listing once the base sort key becomes static. Adding the index is a no-op until rows populate its keys, so it deploys safely ahead of any code change; IAM is already covered by the `SessionsMetadataAccess` `index/*` wildcard. The `tables-detailed` test now asserts all four GSIs.

### Backend — Phase 1a

- `apis/shared/sessions/metadata.py` — `list_user_sessions` now reads the **union** of two disjoint sources: legacy un-migrated rows (base table, `SK begins_with 'S#ACTIVE#'`) and migrated rows (via `SessionRecencyIndex`), so a session is visible whether or not its base sort key has been migrated. Pagination switches to a self-derived value cursor (`{lastMessageAt}#{session_id}`) — each page is computed independently from the last returned position with no cross-page buffering, and fetching `limit+1` valid rows per source provably detects a next page. Undecodable or legacy cursors fall back to the first page (a harmless reset across the deploy boundary). No writes change and no row migrates in this phase.
- The reader **degrades to legacy-only** if `SessionRecencyIndex` doesn't exist yet, so the backend is safe whether or not the CDK GSI has been deployed. This initially caught only `ResourceNotFoundException` (what moto raises); real DynamoDB raises `ValidationException` ("The table does not have the specified index") for a missing GSI, so the catch was broadened (scoped by the "specified index" message) to also degrade on the real error — a 1a backend deployed ahead of the GSI now falls back to legacy listing instead of returning a 503. Verified against the prod table.

## Fixed — "Agent force-stopped" on non-PDF document uploads

Auto prompt caching (`CacheConfig` strategy `auto`) appended its `cachePoint` after the last user message's content, so any turn attaching a non-PDF document (`.txt`, `.docx`, `.csv`, …) sent `[text, document, cachePoint]` — and Bedrock's Anthropic adapter rejected that ordering with `ValidationException … messages.N.content.M.type: Field required`, which surfaced to users as "Agent force-stopped" (prod incidents July 14–16, e.g. a `.txt` transcript upload). Bumping `strands-agents` to **1.48.0** places the cache point *before* the first non-PDF document block instead (upstream issue #1966); every placement it produces was verified live against ConverseStream. The bump also corrects a stale `model_config.py` comment that had credited the wrong upstream PR with this behavior.

## 🚀 Deployment notes

Run `platform.yml` (CDK) to create the `SessionRecencyIndex` GSI, then `backend.yml` (app-api + inference-api) and the frontend deploy. Because the Phase 1a reader degrades gracefully when the GSI is absent, a backend deploy that lands before the CDK deploy will still list sessions (legacy-only) rather than error — but run the CDK deploy so recency listing is ready for the next migration phase. No data migration and no breaking changes; the Word toolset is dark until an admin enables the `create_word_document` capability.

---

# Release Notes — v1.6.1

**Release Date:** July 16, 2026
**Previous Release:** v1.6.0 (July 15, 2026)

---

## Highlights

v1.6.1 is a patch release fixing two agent-invocation regressions. Agents bound to a Mantle-provider model (like `openai.gpt-5.4`) were misrouting to Bedrock and failing with an "invalid model identifier" error, because agent bindings only persist a model id and the invocation path had no provider to key on — it now recovers the provider server-side from the managed-model registry. Separately, every interrupt-resume turn — the OAuth-consent and tool-approval flows, most visibly "connect to Gmail" — was crashing with a 500/424 because a streaming variable was left unbound on the resume path. No infrastructure change and no migration; ships through `backend.yml`.

## 🐛 Bug fixes

- **Agents bound to Mantle models no longer fail with "invalid model identifier."** Agent (assistant) model bindings persist only `model_id`, never `provider`, so previewing or invoking an agent bound to a Mantle model (e.g. `openai.gpt-5.4`) resolved to `provider=None` and misrouted the model to Bedrock ConverseStream — which rejected it, even though the same model works from the normal chat path (which always sends `provider` alongside `model_id`). `_resolve_model_settings` in `apis/inference_api/chat/routes.py` now also returns the model's registered `provider` from the managed-model registry, and the invocation path backfills `effective_provider` from it when the request or binding didn't carry one — fixing all existing provider-less bindings with no data backfill, mirroring how `mantle_api_mode` / `mantle_region` are already recovered. The app-tool-call and app-context-update rebuild paths get the same fallback so a rebuilt agent keys on the same provider as its main turn. On the frontend, the Agent Designer save payload now persists the selected model's `provider` (from the catalog `meta.provider`) alongside `modelId`, so newly created/edited bindings are self-describing (#661)
- **Interrupt-resume turns no longer 500/424.** Resume turns (OAuth-gated MCP consent or tool-approval — `interrupt_responses` set) crashed with `NameError: cannot access free variable 'effective_enabled_tools'`. The variable is referenced unconditionally by the `stream_with_quota_warning` streaming closure (attachment guidance + tabular inventory) but was only assigned on the non-resume branch, so on resume the closure raised before its first yield, the inference-api container returned 500, and the AgentCore Runtime data plane translated that into a 424 Failed Dependency to app-api and the SPA. This broke every interrupt-resume turn since the agent-designer tool-binding refactor — most visibly "connect to Gmail for employees," which completes via an OAuth-consent resume. `effective_enabled_tools` is now bound from the paused-turn snapshot on the resume branch (the same source the resume `get_agent` call uses), with a resume-path regression test in `tests/routes/test_inference.py` (#662)

## 🚀 Deployment notes

No special steps. Both fixes are backend/frontend code only — no CDK deploy, no new AWS resources, no data migration. Ship through `backend.yml` (app-api + inference-api images) and the frontend deploy; the Agent Designer provider-persistence change rides the standard frontend deploy.

---

# Release Notes — v1.6.0

**Release Date:** July 15, 2026
**Previous Release:** v1.5.0 (July 13, 2026)

---

> ⚠️ **Platform (CDK) deploy required.** This release adds a new `shared-conversations` S3 bucket and IAM grants, so it ships through `platform.yml` (CDK) **before** `backend.yml`. No data migration and no breaking changes — legacy inline shares keep working untouched.

---

## Highlights

v1.6.0 makes conversation sharing work for **large** conversations and makes chat **reliable under interruption**. Sharing a big conversation used to fail with a bare 500 because the whole snapshot was inlined into one DynamoDB item past the 400 KB limit; snapshots now offload to a new private S3 bucket, with legacy shares still readable and no migration. On the reliability side, **Stop now actually stops the server-side turn** — a distributed cancellation signal carried over the session lease tears down the running agent instead of letting it burn model and tool spend — and a **per-session single-flight lease** plus a **restore-time history repair** close a nasty class of bugs where a tab switch or duplicate invocation could permanently brick a conversation with a Bedrock tool-pairing error. Rounding it out: web sources are now **removable** and **editor-manageable**, and model RBAC grants written from the model admin page **finally take effect**. Operators must run a CDK deploy first for the new bucket and grants.

## Share large conversations without hitting the DynamoDB item limit

Sharing a large conversation failed with a generic 500 (observed in prod-ai as a `PutItem` ValidationException): `ShareService.create_share` inlined the full message list into a single DynamoDB item, exceeding the 400 KB item limit. Snapshot bodies now offload to a dedicated S3 bucket — mirroring the Memory Spaces / Artifacts / Skills offload pattern — while DynamoDB keeps only control fields plus a `body_ref` pointer. Reads fall back to inline for legacy shares, so existing shares keep working with no migration and the SPA contract is unchanged.

### Backend

- `shares/snapshot_store.py` — new `ShareSnapshotStore`: content-addressed S3 put/get/delete with SSE-S3 and dedupe.
- `shares/service.py` — `create_share` writes the body to S3 and stores a `body_ref` item; `_load_snapshot_body` reads from S3 or falls back to legacy inline items; revoke and session-cleanup best-effort delete the object. `ShareStorageUnavailableError` maps to a friendly 503 instead of a bare 500.

### Infrastructure

- `data/shared-conversations-construct.ts` — new private `shared-conversations` S3 bucket (SSE-S3, versioned) plus an SSM param; threaded to app-api via `PlatformComputeRefs` and the `SHARED_CONVERSATIONS_BUCKET_NAME` env var.
- `app-api/app-api-iam-grants.ts` — `SharedConversationsBucketReadWrite` grant (app-api only).

### Test Coverage

350+ lines: store round-trip / dedupe, a >400 KB regression, S3 and legacy reads, export-from-S3, revoke cleanup, and the storage-unavailable path.

**Related fix (#657):** the shared-conversations *DynamoDB table* was wired into app-api by env var but never granted on the task role, so every share create/list already failed with `PutItem` / `Query` AccessDeniedException surfacing as a 500. Added `SharedConversationsAccess` to the app-api `coreTables` grant list (standard action set on the table and its `index/*` GSIs), with a synth regression test asserting the grant exists and carries no wildcard.

## Stop actually stops the server turn

A client abort — Stop, tab switch, dropped socket — does not propagate through the AgentCore Runtime data plane, so Stop was cosmetic server-side: the container ran the turn to completion, held the session lease, and burned model and tool spend. "Stop → resend" then returned 409 until the prior turn finished on its own. This release reuses the single-flight lease (below) as a cross-container signalling channel so Stop ends the actual turn.

### Backend

- `apis/app_api/sessions/routes.py` — the `user_stopped` endpoint calls `request_session_cancel`, stamping `cancelRequestedFor=<leaseOwner>` on the lease item. Owner-scoped, so a stale Stop can't kill a later turn; best-effort, so it never fails the Stop.
- `apis/shared/sessions/session_lease.py` — the heartbeat (tightened 30s→10s) renews with `ReturnValues=ALL_NEW` and, on `cancelRequestedFor == owner`, flips `session_manager.cancelled`. `acquire` clears any stale cancel marker on takeover.
- `main_agent/streaming/stream_coordinator.py` — two effects: the always-on `StopHook` cancels the next tool call; and a cooperative check at the top of the stream loop raises `_CooperativeStopSignal`, whose handler persists the partial via `_persist_interruption` (marked `user_stopped`), emits terminal SSE frames, and ends cleanly so the client closes and the lease releases. The cooperative arm is what ends a pure-chat turn, which has no tool boundary for `StopHook`.

Net: the 409-on-resend window shrinks from a full turn to ~one heartbeat (10s), and post-Stop spend is halted. **Residual (documented):** an in-flight tool call finishes before cancel is seen, and already-generated Bedrock tokens are billed.

## Duplicate-invocation hardening — no more bricked conversations

Bedrock Converse rejects any history where a user turn's `toolResult` blocks don't exactly match the preceding assistant turn's `toolUse` blocks. A single such violation anywhere in a session's persisted history makes **every** subsequent turn fail, permanently bricking the conversation (this hit prod session `f761f59b`). The trigger was two agent loops running concurrently against one Memory session — spawned by a client reconnect or a duplicate `POST /invocations` the Runtime routed to a different container. This release closes the vector on three fronts.

### Frontend

- `chat-http.service.ts` / `preview-chat.service.ts` — set `openWhenHidden: true` on both `fetchEventSource` call sites so a single stream survives a tab switch instead of the library aborting and reopening it (a fresh `POST /invocations` for the same turn that bypassed the SPA's own double-submit guards). Also correct for long agentic turns (#653).

### Backend

- `apis/shared/sessions/session_lease.py` — new per-session single-flight lease: `acquire` / `renew` / `release` on a dedicated `PK=USER#{uid}, SK=LEASE#{sid}` item via an atomic conditional write. Owner-scoped renew/release; fail-open on any non-conflict DynamoDB error (#655).
- `apis/inference_api/chat/routes.py` — acquire the lease at turn-start and reject a duplicate with 409; resume and max-tokens continuation force-acquire (they re-enter an already-ended loop). Heartbeat renews while the turn streams; release in the generator finally and both except handlers (#655).
- `TurnBasedSessionManager._repair_tool_pairing` — an unconditional restore-time normalizer (sibling to `_strip_document_bytes`) that rebuilds a Bedrock-valid history: one matching result turn per `toolUse` turn (missing ones synthesized as errors), duplicate/orphaned result turns dropped, consecutive same-role turns merged. Identity no-op on healthy history, so it recovers already-corrupted sessions without touching clean ones (#653).
- `persist_synthetic_messages` — a centralized role-alternation guard drops a synthetic "⚠️ Something went wrong" turn that would land adjacent to a same-role turn, killing the consecutive-assistant-message amplifier that turned one errored turn into a permanent brick. Fixes the write side; `_repair_tool_pairing` masks it on the read side (#654).

### Frontend (SPA)

- The inference-api 409 is handled as a soft `AlreadyStreamingError` ("Already responding") notice rather than a hard "Chat Request Failed" toast; loading clears so the user can retry once the prior turn finishes (#655).

## 🐛 Bug fixes

- **Model RBAC grants from the model page now take effect (#651).** The model admin page and the role admin page wrote to two different, unlinked fields: enabling a model for a role on the model page wrote `allowedAppRoles` onto the model record — a field no access check ever read — so the grant silently did nothing and the role page still showed the model unchecked. The role record is now the single source of truth (matching tools and skills): the model form's picker writes through to each role's `grantedModels` (`set_roles_for_model`), `allowedAppRoles` is derived on read (`hydrate_model_roles`), and `can_access_model` / `filter_accessible_models` share one `_grants_access` predicate so a model can no longer be listed by the catalog yet denied on use. Removes the dead `POST /sync-roles` endpoint.
- **Editors can start and view web crawls (#650).** `start_crawl`, `list_crawls`, and `get_crawl` gated on the owner-keyed `get_assistant()`, which returns `None` for an editor-share holder — so the "Add web content" button the SPA already shows editors returned a 404. They now route through the shared `_require_edit_permission` gate (owner|editor), and a viewer gets a clean 403 instead of a misleading 404.

## 🏗️ Infrastructure

- New private `shared-conversations` S3 bucket (SSE-S3, versioned), its SSM param, a `PlatformComputeRefs` entry, the `SHARED_CONVERSATIONS_BUCKET_NAME` app-api env var, and the app-api-only `SharedConversationsBucketReadWrite` grant (#658).
- `SharedConversationsAccess` added to the app-api `coreTables` DynamoDB grant list — standard action set on the shared-conversations table and its GSIs (#657).

## 🔧 CI/CD

- Fixed two pre-existing test failures on develop, both unrelated to the code under test: repointed `test_cache_savings.py`'s five `get_metadata_storage` patch targets to `apis.shared.storage` (the accessor moved; `app_api.storage` is now an empty stub), and re-gated the compaction integration tests on an explicit `RUN_AGENTCORE_INTEGRATION_TESTS=1` opt-in so a mid-suite env-var leak no longer makes them run order-dependently against invalid credentials. Full suite: 4771 passed, 6 skipped (#652).

## 🚀 Deployment notes

1. **Deploy `platform.yml` (CDK) first.** This release adds the `shared-conversations` S3 bucket, its SSM param, and IAM grants (both the bucket read/write grant and the `SharedConversationsAccess` DynamoDB grant on the app-api role). The app-api container reads `SHARED_CONVERSATIONS_BUCKET_NAME` at runtime; deploying app-api before the platform stack would leave sharing large conversations broken.
2. **Then `backend.yml`** to ship the app-api / inference-api images, followed by **`frontend-deploy.yml`** for the SPA (the 409 "Already responding" handling, `openWhenHidden`, web-source delete UI, and model-role picker changes).
3. **No data migration.** Legacy inline shares read back unchanged; new shares offload to S3 automatically. No breaking API changes.

---

---

> ✅ **No platform (CDK) deploy required.** This release is application code and frontend only — it ships through the `backend.yml` (app-api / inference-api image rebuild) and `frontend-deploy.yml` pipelines. No new AWS resources, no IAM changes, no data migration, no breaking changes.

---

## Highlights

v1.5.0 expands the **model catalog** and the **MCP admin** surface, then rounds out the UI. Admins can now run tool **Discovery against OAuth-gated MCP servers** — such as the GitHub remote MCP server — using their own vaulted 3LO token, rather than being refused outright. Two curated model cards land — **Claude Sonnet 5** and **GPT-5.4** — and the **max-output-tokens** field becomes optional so reasoning / Responses-API models that have no fixed output cap can be added at all. The remainder is polish and hardening: sticky admin/settings sidebars, a redesigned 404 page, chat-scroll and sticky-nav fixes, a vitest flake fix, a Mantle Gemma-4 routing fix, and a Docker curl security patch.

## Discover OAuth-gated MCP servers with the admin's token

Admins configuring a tool couldn't see what an OAuth-gated MCP server actually offered: the "Discover" flow refused `auth_type=oauth2` outright (400) or connected unauthenticated and got a 401 (wrapped to a 400), so servers like the GitHub remote MCP server (`api.githubcopilot.com/mcp/`) were undiscoverable. Discovery now connects with the admin's *own* vaulted token for the provider and lists the tools that token can see.

### Backend

- **Provider-aware discovery (#639)** — `MCPDiscoverRequest` gains `requires_oauth_provider` (alias `requiresOauthProvider`). The handler loads the provider, fetches the admin's vaulted 3LO token via AgentCore Identity (`get_token_for_user`) and injects it as `oauth_token` into `create_external_mcp_client` — mirroring how the agent loop attaches the end-user's provider token at runtime, and reusing the exact path `connector_status` already uses. It fetches the **admin's own** token only; it cannot mint an arbitrary end-user's token. Providers such as GitHub scope-filter the tool list to the token's grants, so the result reflects what the admin's connection can actually reach. `requires_consent` → 409; unknown provider / conflict with `forward_auth` / oauth2-without-provider → 400.

### Frontend

- The discover payload now sends `requiresOauthProvider` (the form control already existed) and the `OAuth2CallbackUrl` header (bare `/oauth-complete`, no query string) so the backend can resolve the admin's token.

### Test Coverage

5 backend tests for the OAuth-provider discovery path; 2 SPA specs for the discover payload.

## Model catalog — Sonnet 5, GPT-5.4, and optional output caps

Two new curated cards and one form change together let admins add the current generation of frontier models with a single click.

### Frontend

- **New curated cards (#641)** — **Claude Sonnet 5** (`global.anthropic.claude-sonnet-5`, 1M context, effort-based reasoning, caching on) and **GPT-5.4** (Mantle, `openai.gpt-5.4`, Responses API surface — its `openai.gpt-5.*` id matches the SDK's `/openai/v1` routing prefixes so one-click create routes correctly). The Bedrock Claude list now orders most-capable-first (Opus 4.7, Sonnet 5, Sonnet 4.6, Haiku 4.5), GPT-5.4 sits ahead of Qwen in the Mantle list, and the "Bedrock Mantle" tab moves next to "Bedrock" in the catalog selector.
- **Optional max output tokens (#643, #644)** — newer reasoning / Responses-API models don't publish a discrete max-output-tokens value (output shares the context budget with reasoning tokens), so the admin form field is now optional. `max_output_tokens` becomes `Optional[int]` across `ManagedModelCreate` / `ManagedModel` and the SPA interfaces (`number | null`); the DynamoDB write omits it when absent, the form drops `Validators.required`, and the catalog card null-guards to show "— out". It was only ever a ceiling for the admin-configured `max_tokens` param and is never sent to the provider, so an unset value is safe at inference time.

## 🐛 Bug fixes

- **Mantle Gemma 4 models returned `access_denied` (#641).** Gemma 4 is served only on Mantle's `/openai/v1` path (per its AWS model card), but the Strands SDK's `_OPENAI_PATH_MODEL_PREFIXES` shipped only `openai.gpt-5.`, so `google.gemma-4-*` fell through to `/v1` and inference 401'd. The SDK's prefix table is now extended with `google.gemma-4-` at build time (`_ensure_gemma4_openai_v1_routing`: lazy, idempotent, guarded) until it lands upstream — scoped to the 4.x family so Gemma 3 stays on `/v1`. Covered by guard tests.
- **Sticky sidebars didn't engage (#634).** The redesigned admin/settings sticky sidebars need a real scroll container to anchor against; the app shell now scrolls on the correct element so `position: sticky` takes effect and the chat scroll space sizes to the pending response (#637).
- **Intermittent SPA unit-test flake (#636).** Vitest runs now guarantee the Angular JIT compiler is present, eliminating the sporadic `PlatformLocation` provider error in the unit suite.

## 🔒 Security

- **Docker curl patch floats with the mirror (#645).** Debian removes the superseded point version of curl from the trixie mirror on each security update, so an exact `+deb13uN` pin broke every build once the next CVE landed. Both Dockerfiles now pin `+deb13u*` — tracking the live security patch while keeping the minor version fixed. The digest-pinned base image is what actually provides reproducibility.

## ✨ UI polish

- **Sticky navigation (#632, #638)** — the admin and user-settings sidebar navs stay in view on desktop as the content column scrolls.
- **Redesigned 404 page (#633)** — the not-found page now matches the auth / first-boot screens (frosted glass, animated blobs, graph-paper grid in Boise blue).

## 🔧 CI/CD

- **Portable version sync (#631)** — `scripts/common/sync-version.sh` now runs across GNU and BSD userlands (macOS `sed`/`grep`), so the release bump works outside the dev container.

## 📚 Docs

- Added the quota cooldown-windows + platform-ceiling spec and committee one-pager under `docs/specs/` (#635), and a design note proposing `mantleEndpointPath` as a live admin setting as the durable alternative to patching the SDK's hardcoded routing table (#641).

## 🚀 Deployment notes

- **No special steps.** No CDK deploy, no IAM changes, no data migration, no breaking changes. The backend changes (Mantle Gemma-4 routing, optional `max_output_tokens`, Docker curl pin) ship on the next `backend.yml` image rebuild; the SPA changes ship via `frontend-deploy.yml`.

---

# Release Notes — v1.4.0

**Release Date:** July 10, 2026
**Previous Release:** v1.3.0 (July 10, 2026)

---

> 🏗️ **This release requires a platform (CDK) deploy** — the app-api task role gains two token-vault IAM grants that fix the admin OAuth-provider 502. No data migration, no breaking changes. The new MCP identity-forwarding feature ships **disabled**: enabling it is opt-in and creates a Lambda + pins the Cognito feature plan, so operators who do nothing are unaffected — see the Deployment notes.

---

## Highlights

v1.4.0 adds opt-in **MCP user identity forwarding** and repairs the admin **OAuth-provider** flow. A new Cognito Pre-Token-Generation Lambda can copy configured user-pool attributes into namespaced claims on the access token — the only token forwarded end-to-end to MCP servers — so a personalized MCP tool can identify the calling user without touching the SPA → app-api → inference-api → MCP forwarding path. The feature is disabled by default; a fork that configures nothing gets zero new resources. Separately, adding the first admin OAuth provider returned a 502 Bad Gateway because the app-api task role lacked the token-vault permissions AgentCore needs to lazily create the default vault — now granted.

## MCP user identity forwarding

Personalized MCP tools need to know *who* is calling, but the access token forwarded to MCP servers carried no user attributes. This release adds an opt-in Cognito Pre-Token-Generation v2 trigger that enriches the access token with configured user-pool attributes as namespaced claims — so downstream MCP tools can identify the caller, with no changes to the token-forwarding path (the access token was already the token carried end to end).

### Backend

- **Fail-open enrichment handler (#627)** — `infrastructure/lambda-assets/token-enrichment/handler.py`, a stdlib-only Pre-Token-Generation v2 Lambda. It copies the configured user-pool attributes into namespaced claims on the **access** token. Any error returns the event unchanged, so a misconfiguration can never block login. Covered by `test_handler.py` (244 lines).

### Infrastructure

- **Config surface (#627)** — `McpIdentityConfig` (`infrastructure/lib/config.ts`): `enabled` plus an `accessTokenClaims` map, settable via the `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS` JSON env var or CDK context (new `parseJsonRecordEnv` helper).
- **Conditional construct (#627)** — `token-enrichment-construct.ts` builds the real-code Lambda (`fromAsset`) and attaches it via Cognito `addTrigger` `V2_0`; the user pool's `featurePlan` is pinned to `ESSENTIALS` (a prerequisite of Pre-Token-Gen v2). Wired into `PlatformStack` only when `CDK_MCP_TOKEN_ENRICHMENT_ENABLED=true`, with job-level env in `platform.yml`. A fork that sets nothing gets zero resources and the committed `cdk.context.json` stays inert.

### Test Coverage

244+ lines of new handler tests plus the spec's resolved open questions and an implementation summary (`docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md`, `MCP_USER_IDENTITY_FORWARDING_IMPLEMENTATION.md`), including the mcp-servers follow-on handoff.

## 🐛 Bug fixes

- **Adding the first admin OAuth provider returned a 502 (#628).** `POST /admin/oauth-providers/` failed with a 502 Bad Gateway. The real cause, from dev-ai app-api logs, was an `AccessDeniedException` on `bedrock-agentcore:CreateTokenVault` against `token-vault/default`: AgentCore's `CreateOauth2CredentialProvider` lazily ensures the default token vault exists on the first provider create, which requires `CreateTokenVault` (+ `GetTokenVault`) on the caller. The app-api task role had the `...Oauth2CredentialProvider` actions but not the token-vault ones, and the shared handler maps an uncaught AWS `ClientError` to HTTP 502 — so a missing permission surfaced as a 502 rather than a 403. Both actions are added to the `AgentCoreWorkloadIdentityAccess` statement (the `token-vault/*` scope already covered `token-vault/default`; only the actions were missing).

## 🏗️ Infrastructure

- **New opt-in `token-enrichment` Lambda (#627)** — attached to the Cognito user pool as a Pre-Token-Generation v2 trigger; pins the pool `featurePlan` to `ESSENTIALS`. Created only when `CDK_MCP_TOKEN_ENRICHMENT_ENABLED=true`.
- **app-api task role token-vault grants (#628)** — `bedrock-agentcore:CreateTokenVault` + `GetTokenVault` added to `AgentCoreWorkloadIdentityAccess`.

## 🚀 Deployment notes

- **A platform (CDK) deploy is required** for the OAuth-provider fix (#628) to take effect — it is an IAM change on the app-api task role. Until redeployed, adding the first admin OAuth provider will keep returning a 502.
- **MCP identity forwarding is off unless you opt in (#627).** To enable it: pin the Cognito **Essentials** feature plan on the user pool, then set `CDK_MCP_TOKEN_ENRICHMENT_ENABLED=true` and `CDK_MCP_TOKEN_ENRICHMENT_CLAIMS` (the attribute→claim map) as GitHub Actions variables and redeploy `platform.yml`. Doing nothing leaves the token forwarded exactly as before, with no new resources.
- No data migration, no breaking changes.

---

# Release Notes — v1.3.0

**Release Date:** July 10, 2026
**Previous Release:** v1.2.0 (July 9, 2026)

---

> 🏗️ **This release requires a platform (CDK) deploy** — two IAM grants on the app-api task role back the relocated API-key endpoint. No new AWS resources beyond IAM, no data migration, no breaking changes. Operators with Mantle Responses-only models (e.g. `openai.gpt-5.x`) should set `apiMode=responses` on those model records after deploy — see the Deployment notes.

---

## Highlights

v1.3.0 expands **Bedrock Mantle** support and repairs the **API-key chat endpoint** in cloud. Mantle model records gain two declarative per-model fields — `apiMode` (Chat Completions vs the Responses API) and an optional `region` override — built on Strands 1.47's `bedrock_mantle_config`, which unlocks Responses-only models like `openai.gpt-5.x` and lets a model pin inference to its host region. The API-key `POST /chat/api-converse` endpoint, broken in cloud since the BFF and AgentCore-Runtime migrations, now lives on app-api as a self-contained route and serves the **full model catalog** — Bedrock *and* Mantle — through one shared model builder. Smaller follow-ups: the agent can now read and maintain a Memory Space's `MEMORY.md` index, scheduled runs target Agents (and no longer 403 for regular users), and namespaced Memory Space entry slugs resolve correctly.

## Bedrock Mantle — Responses API and per-model regions

Some Mantle-hosted models only serve OpenAI's Responses API and reject Chat Completions — no endpoint-path knob could ever satisfy them. Admins can now declare, per model, which API a model speaks and which region hosts it.

### Backend

- **Strands-owned Mantle plumbing (#620).** The admin `mantle` provider now rides Strands' `bedrock_mantle_config`: the SDK owns the base URL, the model-family base path, and bearer-token minting (via the new `aws-bedrock-token-generator` dependency), replacing the hand-rolled inference plumbing. Two declarative per-model fields cover what the library can't infer:
  - `apiMode` (`chat` | `responses`) — selects `OpenAIModel` vs `OpenAIResponsesModel`. The Responses API uses different native param names, so `to_mantle_config` selects a Responses-specific param map (`max_output_tokens`, nested `reasoning.effort`) by mode.
  - `region` — optional override driving both the Mantle endpoint host and the SigV4 region the bearer token is signed for, so a model can pin inference to its host region (e.g. `gpt-5.x` in `us-east-1`) independent of where the app runs.
- The runtime fields (`mantle_api_mode` / `mantle_region`) thread through `model_config`, the agent factory, `base_agent`, the paused-turn snapshot, `stream_coordinator`, and the chat service/routes, so bound agents and resumed turns honor them end to end (#620).
- **`mantleEndpointPath` is deprecated** — accepted-but-ignored in the schema so no stored record breaks, and removed from the admin UI and runtime. Gemma 4 (`google.gemma-4-31b`) is temporarily un-curated: it needs the `/openai/v1` base path but the SDK only routes `openai.gpt-5.*` there; it returns once the `google.gemma-` family prefix lands upstream (#620).
- **Dependency step (#619):** `strands-agents` 1.40.0 → 1.47.0 (with the `[bidi]` extra) plus `aws-bedrock-token-generator` 1.1.0, resolver-confirmed against `strands-agents-tools` 0.5.2. Full backend suite green on the new pin.

### Test Coverage

Backend suite green at 2,342 tests on the refactor; frontend typecheck + manage-models specs green.

## API keys work in cloud again — `/chat/api-converse` on app-api, full catalog

The programmatic API-key endpoint was broken in every deployed environment: app-api proxied `POST /chat/api-converse` to inference-api, but inference-api now runs inside an AgentCore Runtime whose data plane only serves `POST /invocations` and `GET /ping` — every other path returns `UnknownOperationException` before reaching the container. It worked locally only because `localhost:8001` bypasses the runtime gateway.

### Backend

- **Self-contained app-api route (#621).** The handler moves onto app-api (validate key → RBAC → Bedrock converse → cost accounting), reaching Bedrock directly via the task role — no inference-api hop, no `INFERENCE_API_URL` dependency. The proxy, the dead inference-api route, and its DTOs are deleted; verified with an un-mocked smoke returning 200 for stream and non-stream.
- **Mantle models now work over API keys (#621).** The handler was Bedrock-only — `provider="mantle"` models 400'd. Mantle model construction (class-pick + `bedrock_mantle_config` + param maps) is extracted to `apis/shared/models/mantle.py` as `build_mantle_model`, shared by the agent factory and the API-key handler (app-api can't import `agents/`). The handler resolves the requested model's provider from the catalog and branches: Bedrock → boto3 converse (unchanged); Mantle → the shared builder's bare Strands `.stream()`, which yields the same Converse-shaped events — so SSE translation and usage/cost accounting are one code path, and cost records now carry the real provider. Unknown ids fail safe to the Bedrock path. Verified with live dev smokes: chat-mode Mantle and Responses-API Mantle (`openai.gpt-5.4`) both 200 (stream + non-stream).

### Frontend

- **API-key snippets point at the right URL (#621).** After the BFF refactor, CloudFront only routes `/api/*` to the backend; the generated curl/Python/JS examples emitted the bare origin, producing a CloudFront 403. The settings page now resolves a relative/empty `appApiUrl` against the current origin (`<origin>/api/chat/api-converse`), leaving local dev's absolute `http://localhost:8000` untouched.

### Infrastructure

- **app-api IAM (#621):** the Bedrock invoke statement gains `bedrock:InvokeModelWithResponseStream` and broadens to all-region foundation models plus the account-level inference-profile ARN (the catalog uses `us.*` cross-region profiles), mirroring inference-api's grant; and the project-scoped Mantle statement (previously browse-only `Get*`/`List*`) gains `bedrock-mantle:CreateInference`.

## ✨ Improved

- **Scheduled runs target Agents (#615).** The schedule form's target selector lists Agents (the Designer primitive superseding Assistants — same record, `agentId == assistantId`, so the wire field is unchanged). Because an Agent's tool bindings *replace* the run's `enabled_tools` at invocation, the manual tool picker now hides when an Agent is selected — it previously offered tools that would be silently discarded — and "Run now" actually targets the selected Agent (it previously ignored it).
- **The agent can maintain a Memory Space's index (#614).** `MEMORY.md` — the human-readable index hydrated into the agent's context each session — was write-only from the agent's perspective, so it could silently drift from the entries the agent writes. The reserved `MEMORY.md` slug (case-insensitive) now routes `memory_read` → `read_index` (viewer+) and `memory_write` → `update_index` (editor+), with the same readwrite-binding gate as entry writes. The slug is reserved; it can't become an ordinary entry.

## ⚠️ Changed

- **Scheduled Runs are un-gated from RBAC (#617).** The `scheduled-runs` capability check is dropped from the `/schedules` and `/runs/*` gates — only the `SCHEDULED_RUNS_ENABLED` kill switch remains (404 when off). This widens who can *reach* the surface, not what any caller can do: runs still execute with the caller's own RBAC-allowed tools. The feature stays deliberately low-key — no nav entry, reachable by direct URL. `apis/shared/rbac/capabilities.py` remains in place so re-gating is a two-line revert.

## 🐛 Bug fixes

- **Regular users saw a 403 "Access Denied" toast on page load (#617).** The sidenav ran a background `loadSchedules()` probe on every load; the beta-cohort RBAC gate 403'd it and the global error interceptor popped the toast before the service's graceful catch ran. Fixed by the un-gating above plus removing the vestigial probe (the template never rendered a schedules link).
- **Namespaced Memory Space entries 404'd (#614).** Entry slugs are namespaced with a slash (e.g. `people/brian-bolt`), but the entry routes declared a plain `{slug}` param whose converter stops at `/` — and Uvicorn percent-decodes `%2F` before routing, so view/edit/delete never matched. The GET/PUT/DELETE routes now use the `{slug:path}` converter.

## 📦 Dependencies

| Component | Package | From | To |
|---|---|---|---|
| Backend | `strands-agents` (+ `[bidi]`) | 1.40.0 | 1.47.0 |
| Backend | `aws-bedrock-token-generator` | — | 1.1.0 (new) |

## 🚀 Deployment notes

Deploy order: **platform (CDK) → backend → frontend.**

- **Platform deploy is required** for the two app-api IAM grants (#621). Without them, the relocated `/chat/api-converse` AccessDenies on streaming/inference-profile Bedrock models and on all Mantle models.
- **Mantle model records:** after deploy, set `apiMode=responses` on any Responses-only Mantle model (e.g. `openai.gpt-5.4`) in the admin model manager — records default to `chat`. `mantleEndpointPath` is now ignored; no cleanup needed. Optionally set `region` on models whose host region differs from the app's.
- **Scheduled Runs** become reachable (by direct URL) to all users where `SCHEDULED_RUNS_ENABLED` is on; set it to `false` to turn the surface off entirely.
- No data migration and no breaking API changes.

---

# Release Notes — v1.2.0

**Release Date:** July 9, 2026
**Previous Release:** v1.1.0 (July 8, 2026)

---

> This is a code-only release (no new AWS resources). It ships through the standard **backend** and **frontend** pipelines. One operator-facing flag flips default (`AGENTS_API_ENABLED` is now on) — see the Deployment notes at the end of this entry. No migration.

---

## Highlights

v1.2.0 completes the **Agent Designer** that landed dark in 1.1.0. Skill bindings now resolve at invocation — joining model and tool bindings so an Agent fully governs its model, parameters, tools, and skills against the *invoking* user. The chat input now reflects those governed bindings honestly (locked pickers with "Set by agent" affordances), the Designer gains a **live side-by-side preview** that streams the saved agent through the real invocation path, model **parameters** are governed at author time, and the full knowledge-base editor is now available on an Agent — closing the last Assistant→Agent migration blocker. With the feature complete, the `/agents` API flips **on by default** (still admin-gated "Preview" in the nav). There are no breaking changes and no migration.

## Agent Designer — completion

1.1.0 shipped the Agent Designer contract, `/agents` surface, bindable-primitives catalog, and Phase-3 harness resolution for the model and tool bindings, all behind a default-off flag. This release finishes the story.

### Backend

- **Skill bindings resolve at invocation (#602).** `resolve_agent_invocation` now returns `plan.skills` (`ResolvedSkills`). When an Agent binds skills they *replace* the request's skills for the turn and the route forces `agent_type="skill"` so the SkillAgent discloses exactly the bound set. Each bound skill is re-checked against the invoking user via `AppRoleService.can_access_skill`; a missing skill — or the Skills feature being disabled in the environment — blocks the turn with a message. No skill binding ⇒ `plan.skills is None` ⇒ the request's `agent_type`/`enabled_skills` drive the turn exactly as today. Resolution reassigns `effective_agent_type`/`effective_skill_ids` before the main-turn `get_agent`, so a bound-skill agent resumes on the same `skills_hash` (resume-safe, mirroring the tool slice). Design-time, `skill` drops from the inert kinds — no inert kinds remain — and a bound skill is flag-gated and validated against the author's palette.
- **Model-parameter governance (#609).** `binding_validation._validate_model_params` rejects params that are unsupported, locked, or out of `[min,max]`/allowed-set against the model's admin `supported_params` — an author-facing 400 instead of a silent runtime clamp (belt-and-suspenders to the runtime merge).

### Frontend

- **Live editor preview (#609).** A new `AgentPreviewComponent` reuses `PreviewChatService` to stream the *saved* agent through the real `/chat/stream` invocation path, so all bindings (model / params / tools / skills / memory) resolve server-side. Agents send a minimal request body and opt out of the assistant preview's `system_prompt` + owner-tools injection (which fought the bindings and could blow the 8 KB system-prompt cap on long personas); a capability strip and dirty banner make the resolved context and save-to-apply semantics explicit. A two-column editor shell mirrors the assistant editor.
- **Data-driven Parameters subsection (#609)** under the model picker reads `meta.supportedParams` (numeric inputs, enum selects, locked read-only); empty params omit `params` entirely, preserving today's resolution.
- **Chat input reflects governed bindings (#603).** The model/tool/skill pickers lock to the active Agent's bindings (locked read-only chip "set by this agent", "Set by agent" model row, and a "This agent uses a fixed set of tools/skills" banner with disabled toggles). This is UI honesty — the backend remains the authority — and it is per-primitive: an Agent that binds a model but no tools locks only the model. When an agent locks the panel, it now lists **only** the bound tools/skills rather than the full accessible set greyed out (#606).
- **Knowledge base in the Designer (#608).** The assistant editor's inline KB section is extracted into a reusable `KnowledgeBaseSectionComponent` and used in both the assistant form and the agent form, replacing the agent's read-only "managed automatically" card with the live document / web-crawl / connector flow. This closes the last Agent migration blocker; the gap was frontend-only, since the document pipeline already keys on the record id and `agentId == assistantId`. The component owns record identity via a `createDraft` callback (the assistant form sheds ~1000 lines), and a `permissionResolved` gate keeps a viewer from 403-ing on edit-only sync-policy calls.

## ✨ Improved

- Settings panel shows only an agent's bound tools/skills when it locks the toolset, instead of the full list with unbound entries greyed out (#606).

## ⚠️ Changed

- **`AGENTS_API_ENABLED` now defaults on.** The Agent Designer is complete, so the flag flips from opt-in to default-on (empty-string-safe — only the literal `false` disables), matching `scheduled_runs`/`memory_spaces`. The `/agents/*` API now responds in every environment. **SPA nav stays preview-gated** (system-admin + "Preview" badge), so this does not broaden user-facing exposure — it just stops the API 404-ing per-environment (#607).
- **Memory Spaces and Scheduled Runs are temporarily hidden from the side nav** while the Agent Designer is the focus. Their routes, pages, and capability probes are unchanged — re-enabling is just restoring the nav template blocks. The features remain deployed and reachable; they're only absent from navigation (#611).

## 🐛 Bug fixes

- **Picker locks stuck after "New chat."** The agent-binding lock release lived inside a guard that was false on the freshly-recreated session component, so the model/tool pickers stayed locked to the previous agent conversation. The release now always runs when no assistant is in the URL (idempotent) (#603).
- **Preview showed the wrong model.** The Designer preview reused the main chat input, which read the user's global model and let them switch it even though the harness resolves the model from the agent's binding. The preview's model picker is now locked to the agent's bound model, released on destroy (#611).
- **Flaky cadence test.** The scheduled-runs re-arm test asserted a daily-9am delta in `(1h, 48h)`, which failed legitimately when CI ran in the hour before 9am Boise. The dispatcher clock is now frozen so the assertion is time-of-day independent (#608).

## 🏗️ Infrastructure

- CDK `config.agents.enabled` now defaults on (`!== 'false'` with a `?? true` context fallback), mirroring `memorySpaces`/`scheduledRuns`. No new AWS resources — the `AGENTS_API_ENABLED` env var only gates whether `/agents/*` responds (#607).

## 🚀 Deployment notes

Code-only release — no new infrastructure and no data migration; it ships through the standard **backend** and **frontend** pipelines (a platform/CDK deploy is only needed to pick up the `config.agents.enabled` default flip, which is otherwise inert).

- **`AGENTS_API_ENABLED` is now on by default.** After deploy, the `/agents/*` API responds in every environment. This does **not** expose the Agent Designer to end users — the SPA nav entry remains system-admin-only with a "Preview" badge. To keep the API dark in an environment, set `CDK_AGENTS_API_ENABLED=false` and redeploy the platform.
- **Memory Spaces / Scheduled Runs disappear from the side nav.** This is expected (#611); the features are still deployed and their routes still resolve. Re-enabling is a template-only change in a future release.

---

# Release Notes — v1.1.0

**Release Date:** July 8, 2026
**Previous Release:** v1.0.4 (July 1, 2026)

---

> 🏗️ **This release adds new AWS infrastructure** (a dedicated Memory Spaces table + bucket, two new Lambda images, and several GSIs), so it ships through the **platform (CDK)** pipeline, not the API-only backend path. There is no data migration and no breaking change — existing deployments upgrade in place. See the Deployment notes at the end of this entry.

---

## Highlights

v1.1.0 is the platform's first **feature** release since the 1.0 line, turning AgentCore from a reactive chat surface into an **agentic platform**. It lands four new capabilities on shared, RBAC-governed primitives:

- **Scheduled Runs** — agents that run *unattended* on a cadence (or on demand) and deliver a session you can read later.
- **Memory Spaces** — named, templated, shareable markdown "second brains" you fully own (export the whole thing as a `.zip`).
- **Agent Designer** — an authoring surface that composes an Agent from governed primitives (model, tools, skills, knowledge bases, Memory Spaces).
- **Knowledge Base Sync** — assistant knowledge sources (Google Drive + web crawls) that re-index themselves on a schedule.

Alongside them is a **chat/session UX overhaul**: streaming is now isolated per conversation (concurrent chats no longer stomp each other), stopped or dropped turns persist their partial reply and cost, session titles stream in mid-response, and the top-nav gains a session-options menu and an active-assistant pill.

Everything new is flag-gated and back-compatible. **Scheduled Runs, Memory Spaces, and KB Sync default on; the Agent Designer defaults off.** The three preview surfaces (Agents, Memory Spaces, Scheduled Runs) are gated to system-admins and marked with a "Preview" badge while they mature. **Operators must run a platform (CDK) deploy** for the new resources — see the deployment notes.

---

## Scheduled Runs

Agents can now run **without a person watching** — on a schedule you set (daily, on weekdays, weekly, or every *N* minutes/hours) or on demand via "Run now." A run executes one agent turn as the owner, with the owner's RBAC, and delivers the result as a normal session that shows an unread dot in the sidebar until you open it. This is the keystone of the proactive-agent effort (F1–F3 in the primitives plan).

The hard problem was **unattended auth**: a headless run has no live user session to sign requests. The chosen path is an explicit, revocable *headless-grant* record created when a user enables scheduled runs from an attended session; the worker mints a per-owner Cognito bearer from it at fire time (the workload-token and SigV4 front-door paths were both proven dead at the runtime gateway during the F1 spike).

### Backend

- `apis/shared/harness/` — `run_agent_headless()` mints a per-owner Cognito bearer, drives the runtime's `/invocations` endpoint, drains the SSE stream server-side, and lets the runtime materialize + title the delivered session. Ships with an audit-only, fail-closed governance floor and wired no-op guardrail/classification seams (#560, #561).
- `apis/shared/harness/grants.py` — headless-grant lifecycle: create-on-enable, per-owner lookup via the sparse `HeadlessGrantUserIndex`, rotation-aware minting (a rotated refresh token is persisted before the mint returns), and revocation that deletes the stored credential. TTL is anchored to the login that issued the pinned refresh token ("must have logged in within 30 days") (#561).
- `apis/shared/scheduled_prompts/` + `apis/app_api/runs/` and `.../schedules/` — `ScheduledPrompt` model and service (cadence → `next_run_at` computed timezone-aware), CRUD under `/schedules`, and the `/runs/now` + `/runs/grant` surfaces. Gated by the `SCHEDULED_RUNS_ENABLED` kill switch **and** a new `scheduled-runs` RBAC capability resolved through the mature tools grant axis (#561, #563, #578).
- Dispatcher + worker (`rate(5m)` EventBridge → sweep the sparse `DueScheduleIndex` → runaway guard → conditional re-arm → fire-and-forget → per-owner mint → `run_agent_headless(trigger="schedule")` → record outcome, pausing on `reauth_required` / `oauth_required` / repeated failures). A persistent `consecutive_failures` counter trips the breaker at the production default of 3 (#565).
- **Security:** client-supplied `enabled_tools` is intersected with the caller's resolved RBAC grant at schedule create, schedule update, and Run-now (`AppRoleService.filter_requested_tools`), so a scheduled run can never persist a tool outside the owner's role (#568). Schedule edits gained explicit `clearAssistant` / `clearTools` flags so a `null` no longer silently reads as "leave unchanged" (#569).

### Frontend

- `frontend/ai.client/src/app/schedules/` — a signal-based list/create/edit page (bounded cadence UI + IANA timezone, optional assistant + tool snapshot, pause/resume/delete with confirm), the enablement UX (status banner, "Enable scheduled runs", reauth affordances), and "Run now" wired to a new app-wide `BackgroundTaskService` + toast component mounted in `app.html` (#564, #578).
- The "Scheduled Runs" nav entry is gated to system-admins (`showSchedules() && isAdmin()`) and carries a "Preview" badge (#574, #600).
- Delivered runs surface an unread dot (durable server flag ORed with the in-tab signal); the session menus gained a Mark-as-read / Mark-as-unread toggle (#572, #577).

### Infrastructure

- Two new sparse GSIs on the sessions-metadata table — `DueScheduleIndex` (projected only while a schedule is active) and `HeadlessGrantUserIndex` (only `HEADLESS-GRANT#` items carry the partition attribute). App-api's existing table grant already covers `index/*`, so no IAM change was needed for the schedule surface (#561, #563).
- `backend/Dockerfile.scheduled-runs` — a lean dispatcher+worker image (no ML deps) sharing one image via `ImageConfig.Command`, deployed via the platform-as-bootstrap pattern. Worker IAM scoped to sessions-metadata RW, the grant table, app-client secret read, and AgentCore vault token + oauth-secret read (#565).

### Test Coverage

Backend resolver/route/harness suites (schedule CRUD, cadence math, RBAC intersection, grant lifecycle, audit fail-closed ordering, MockTransport stream outcomes) plus an AST guard asserting the lean image never imports `agents`/`strands`; frontend schedule-form, run-now, and background-task specs.

---

## Memory Spaces

Memory Spaces are named, first-class **markdown wikis** — a `MEMORY.md` index plus typed entries (entity / episodic / fact) — that a user owns, templates from, and shares. They're the bindable memory primitive behind the "Oliver / Chief-of-Staff" use case, but generalized: any user can create one, and any Agent can bind one. The headline ownership property is a **loss-free `.zip` export** of the entire space (structure preserved, re-importable), so there is zero lock-in.

### Backend

- `apis/shared/memory/` — an S3 content-addressed byte store (sibling of the skills store); `MemorySpace` / `MemoryIndex` / `MemoryEntryRef` / `SpaceMember` models; Blank / Chief-of-Staff / Research-Notebook templates; a dedicated memory-spaces table repository (META/INDEX/MEMBER rows, Decimal-safe); and a permission-gated service whose `resolve_permission` chokepoint enforces viewer-reads / editor-writes / owner-shares-and-deletes with content-addressed writes and GC-on-replace (#582).
- `apis/app_api/memory_spaces/` — `/memory/spaces` CRUD (list with per-space role, create-from-template, entry/index I/O, delete-or-leave), `/shares` grant CRUD, `/export` (streams a `SpooledTemporaryFile` zip that spills to disk beyond 8 MiB, sanitized against zip-slip), and `/consolidate` (a deterministic health pass that GCs orphaned objects and *reports* dup/dead-link/over-cap findings without mutating durable memory). All 404 while `MEMORY_SPACES_ENABLED` is off (#584, #585, #586, #589).
- **Optimistic concurrency** makes multi-editor spaces safe: `put_index(expected_version=…)` does a conditional DynamoDB write on the manifest version, and entry writes route through a bounded read-modify-conditional-write retry loop that converges on transient races and raises `409` only on a sustained one (#586).

### Frontend

- `frontend/ai.client/src/app/memory-spaces/` — a list page (owned + shared-in cards with role/template badges), a detail page (edit `MEMORY.md` and the entry list; entries open in a view/edit dialog), a create-from-template dialog, and a share dialog (add-by-email + per-row role, delta-on-save). Viewer access is read-only throughout (#587).
- The "Memory Spaces" nav entry rides a live `accessible$` 404-probe, is gated to system-admins, and carries a "Preview" badge (#587, #600).

### Infrastructure

- `MemorySpacesConstruct` — a content-addressed S3 bucket + a dedicated `memory-spaces` DynamoDB table with `OwnerIndex` + `MemberIndex` GSIs, threaded to both compute roles (readwrite S3 + DynamoDB) with `S3_MEMORY_SPACES_BUCKET_NAME` / `DYNAMODB_MEMORY_SPACES_TABLE_NAME` / `MEMORY_SPACES_ENABLED` env vars. Provisioned unconditionally; only the runtime flag gates route mounting (#582, #588, #597).

### Test Coverage

47 moto-backed data-layer tests plus route suites covering CRUD, share matrices, zip layout/verbatim-frontmatter/hostile-slug, optimistic-lock convergence, and the consolidation report; frontend facade specs.

---

## Agent Designer

The Agent Designer is a new authoring surface that composes an **Agent** from RBAC-governed primitives — a governed single-select model plus a uniform `bindings[]` array (`tool` | `skill` | `knowledge_base` | `memory_space`) — evolving the existing Assistant store *in place* rather than forking a parallel table. Legacy Assistants read as Agents through a compat mapping, and `/assistants/*` is unchanged. It ships **dark** (`AGENTS_API_ENABLED` default off) so environments can opt in for dogfooding.

The governing design decision: RBAC is *composed* from the five existing per-primitive access checks, not reinvented, and bindings are resolved twice — filtered at design time against the author's palette, then re-resolved at run time against the **invoking** user with block-on-missing.

### Backend

- `apis/shared/assistants/` — `AgentModelConfig` (governed single-select; stored as `modelConfig`) and `AgentBinding[]`, both optional/additive; a compat mapping projecting a legacy Assistant to an Agent (synthesizing a `knowledge_base` binding, never fabricating a model); Decimal-safe serialization for `modelConfig.params` (#591).
- `apis/app_api/agent_designer/` — the governed `/agents/*` surface (draft/create/list/get/update/delete + shares) returning the Agent shape, and `/agents/bindable?kind=…`, an RBAC-filtered palette composing the five per-primitive access services. Design-time `binding_validation` composes model access, memory `resolve_permission`, and shape-only checks; the model and tool write-checks use the *same* predicate the picker's catalog uses, so "if the palette offers it, the write accepts it" holds by construction (#592, #598, #600, #601). (Package renamed from `agents` to `agent_designer` to avoid shadowing the top-level `agents` package on the app-api `sys.path` — #595.)
- **Phase 3 harness resolution** (in inference-api, importing `apis.shared` only): `resolve_agent_invocation()` re-resolves the Agent's `modelConfig`, bound Memory Space, and `tool` bindings against the invoking user. A pinned model / memory grant / bound tool is re-checked per invoker; a missing grant blocks the turn with a conversational message (no silent downgrade). Bound tools *replace* the request's `enabled_tools`; the bound Memory Space's `alwaysLoad` content is injected into the system prompt and exposes `memory_list` / `memory_read` (always) + `memory_write` (readwrite bindings) tools (#594, #596, #601).

### Frontend

- `frontend/ai.client/src/app/agents/` — an Agents list page and an agent-form (persona/emoji/tags/starters, required single-select model picker, tool/skill multi-select chips, a Memory Space picker with read / read+write access and an `alwaysLoad` toggle, read-only KB). Sharing reuses the assistants share dialog. The "Agents" nav entry is gated to system-admins with a "Preview" badge (#598, #599, #600).

### Infrastructure

- `AGENTS_API_ENABLED` wired through CDK config (`CDK_AGENTS_API_ENABLED`, default off) onto app-api and the inference runtime. No new AWS resources — the flag only gates whether the routes 404 (#593).

### Test Coverage

Contract/compat/persistence/router suites, the bindable-catalog matrix, and per-invoker resolver cases (override, block-on-missing, dedupe, passthrough) for model, memory, and tool bindings.

---

## Knowledge Base Sync

Assistant knowledge sources previously indexed once, at import. KB Sync keeps them **fresh**: imported Google Drive files and web crawls can be put on a schedule (Daily/Weekly/Monthly) that re-checks the source, re-embeds only what actually changed, and pauses gracefully when a credential needs re-consent. It's a "sweeper, not scheduler" design — a periodic sweep of due policies with layered runaway guards.

### Backend

- `apis/shared/sync_policies/` — `SyncPolicy` model + repository (adjacency-list items on the assistants table) with a sparse `DueSyncIndex` (keyed only while active, so paused policies are invisible to the sweep), conditional re-arm for idempotent dispatch, and breaker counters. Assistant/document deletes cascade their policies (#542).
- Dispatcher + worker (`rate(15m)`): the dispatcher applies guards in order (kill switch → liveness → circuit breaker → 30-day inactivity → in-flight skip → re-arm-with-backoff), then async-invokes the worker. The worker resolves the policy creator's Google token from the AgentCore Identity vault (no live session), does two-gate change detection (Drive `version` then content hash) for Drive files and conditional-GET/ETag + content-hash for web crawls, and stages changed bytes to the existing S3 key so the untouched ingestion pipeline re-chunks/re-embeds. Stale tail vectors are cleaned up on shrinkage (#543, #544, #545).
- `/assistants/{id}/sync-policies` CRUD + run-now (202, atomic 10-min cooldown) + resume hooks: a `paused_reauth` policy resumes only on a fresh OAuth consent; a `paused_inactive` policy wakes on a throttled `lastUsedAt` bump from chat use (#546).

### Frontend

- Per-source "Keep in sync" controls on the assistant knowledge editor (interval select, status line with state/reason/last-and-next sync, pause/resume, cooldown-aware Sync now, and a Reconnect affordance that routes through the OAuth consent popup). Owner/editor-only; device uploads show no control (#547). Follow-on UX polish clarified the control copy, always shows last-synced, adds a saving indicator, and unifies skeleton loading (#552, #555).

### Infrastructure

- `backend/Dockerfile.kb-sync` — a lightweight dispatcher+worker image (boto3 + pydantic, no ML deps) sharing one image via `ImageConfig.Command`, deployed via platform-as-bootstrap; a `rate(15m)` EventBridge rule; error alarms; function-name SSM params for the code-deploy step. Worker IAM adds read on the vault's backing OAuth secrets (`…!default/oauth2/*`) — the same grant app-api/inference-api carry, minus the write lifecycle (#543, #549).

### Test Coverage

Dispatcher-guard ordering, Drive/web change-detection and pause-semantics matrices, TTL-rearm cases, and the import-surface simulation that keeps the lean image FastAPI-free.

---

## Chat & session UX

A cluster of changes that make multi-conversation chat robust and legible.

- **Per-conversation streaming (#535).** `ChatStateService` now holds per-session state (loading, stop reason, cost/context aggregates, Continue affordance, `AbortController`) behind a viewed-session facade; `StreamParserService` keys a `Map<sessionId, ParserSessionState>` and drops late events from a superseded stream. Asking in conversation A then navigating to B and asking again no longer crosses streams, and navigating away no longer aborts the in-flight turn (the backend still completes and persists it). Adds per-conversation scroll restore, a streaming-text replay fix, and a sidebar in-progress dot.
- **Interrupted-turn persistence (#541, #548).** A Stop / refresh / dropped socket used to orphan the user turn (no assistant reply persisted). Now the in-flight partial text is persisted via `asyncio.shield`, an authoritative `POST /sessions/{id}/interrupt` carries the `user_stopped` reason, and `_persist_interruption` also writes per-message + session-aggregate metadata (with a context-attribution projection for input tokens when a cut turn never delivered Bedrock's usage event) — so cost badges and the reload "Continue" chip survive.
- **Mid-stream session titles (#540).** inference-api interleaves a one-shot `session_title` SSE event once the concurrent title task resolves; the SPA applies it to both the sidebar and the top-nav header. Fixes a latent bug where title generation ran sync `boto3` on the event loop and stalled the live stream.
- **Top-nav session menu + active-assistant pill (#538).** The top-nav title gains a dropdown (Rename / Share / Save / Delete) and an active-assistant pill moved out of the chat-input footer, with optimistic rename and a title-transition polish pass.
- Other: conversation export defaults to messages-only (#537); OAuth consent state is cleared on session switch (#539); the tool card is preserved after an OAuth/tool-approval resume (#532); the assistant editor groups its Knowledge Base into an inset panel (#557).

---

## 🐛 Bug fixes

- **Admin "Last login" / "Created" showed "Never" in Safari.** `UserSyncService` built timestamps as `datetime.isoformat() + "Z"`, yielding an invalid `…+00:00Z` (offset *and* Z) that strict engines parse to Invalid Date. Normalized write-path timestamps to a single trailing `Z` and added a read-path heal for legacy rows (#556).
- **Admin model list returned a generic 502.** `GET /admin/bedrock/models` calls `ListFoundationModels`, but the app-api task role only had `InvokeModel`, so deployed environments hit `AccessDenied` (it only worked locally under broader dev credentials). Granted `bedrock:ListFoundationModels` + `bedrock:GetFoundationModel` (#571).
- **OAuth consent banner leaked across sessions.** The root-singleton consent service was keyed by `providerId`, not `sessionId`, and wasn't reset on conversation change; now cleared fail-closed alongside the other per-session resets (#539).
- **Tool card vanished after an OAuth/tool-approval resume.** The resumed stream omits the original `tool_use` block; both resume paths now pin the existing messages as a prefix and reconcile from persisted memory after the stream closes (#532).

## 🏗️ Infrastructure

- New dedicated `memory-spaces` DynamoDB table (`OwnerIndex` + `MemberIndex` GSIs) and content-addressed S3 bucket (`MemorySpacesConstruct`).
- New sparse GSIs: `DueScheduleIndex` and `HeadlessGrantUserIndex` (sessions-metadata table), `DueSyncIndex` (assistants table).
- Two new lean Lambda images with EventBridge sweeps, both platform-as-bootstrap: `Dockerfile.scheduled-runs` (`rate(5m)`) and `Dockerfile.kb-sync` (`rate(15m)`).
- New empty-string-safe kill-switch flags forwarded via `platform.yml`: `SCHEDULED_RUNS_ENABLED`, `MEMORY_SPACES_ENABLED`, `KB_SYNC_ENABLED` (default on), `AGENTS_API_ENABLED` (default off).
- IAM additions: `bedrock:ListFoundationModels`/`GetFoundationModel` on the app-api task role; kb-sync worker read on the vault-backing OAuth secrets; scheduled-runs worker scoped to sessions-metadata RW + grant table + app-client secret + AgentCore vault token/oauth-secret read.

## 🔧 CI/CD

- `backend.yml` gains per-image build + API-driven code-deploy jobs for the kb-sync (dispatcher/worker) and scheduled-runs (dispatcher/worker) images, each sharing one image tag; `deploy-image-lambda-one.sh` grows a first-deploy grace skip for the introducing PR. Nightly image-scan and supply-chain Dockerfile-pinning lists include both new Dockerfiles.

## 📦 Dependency notes

No core backend (`pyproject.toml`) or frontend (`package.json`) dependency versions changed in this release. The new pins live **only** in the two lean Lambda images:

| Component | Package | Version |
|---|---|---|
| kb-sync (web re-crawl) | `beautifulsoup4` | 4.13.5 |
| kb-sync (web re-crawl) | `trafilatura` | 2.0.0 |
| kb-sync (web re-crawl) | `lxml` | 6.1.1 |
| scheduled-runs (worker) | `cryptography` | 48.0.1 |
| scheduled-runs (worker) | `cachetools` | 6.2.4 |
| shared image runtime | `httpx` | 0.28.1 |
| shared image runtime | `bedrock-agentcore` | 1.9.1 |
| shared image runtime | `boto3` | 1.43.9 |
| shared image runtime | `pydantic` | 2.12.5 |

## 🚀 Deployment notes

Unlike the recent 1.0.x patches, **this release requires a platform (CDK) deploy** — it creates a new DynamoDB table, a new S3 bucket, several GSIs, two new Lambda images, and two EventBridge rules. Run the `platform.yml` pipeline first, then the `backend.yml` pipeline ships the real kb-sync and scheduled-runs image code (platform-as-bootstrap). No data migration is needed and there are no breaking API changes; deployments on any 1.0.x upgrade in place.

Feature enablement after deploy:

- **Scheduled Runs, Memory Spaces, KB Sync** are **on by default** (empty/unset workflow vars = on; only the literal `false` disables). Their nav entries are visible only to system-admins and marked "Preview." Grant the `scheduled-runs` RBAC capability to the roles that should be able to schedule runs.
- **Agent Designer** is **off by default**. Set `CDK_AGENTS_API_ENABLED=true` for an environment to dogfood the `/agents` surface (its full payoff needs Memory Spaces deployed in the same environment).
- To dark-stop any preview surface in production, set the matching `CDK_*_ENABLED=false` and redeploy the platform.

---

# Release Notes — v1.0.4

**Release Date:** July 1, 2026
**Previous Release:** v1.0.3 (June 30, 2026)

---

> ⚠️ **Upgrading from a beta?** 1.0.4 is an in-place upgrade from any 1.0.x with no migration. Moving from a pre-1.0.0 beta is still the destructive backup → teardown → redeploy → restore migration described in the [1.0.0 notes](#upgrading-an-existing-deployment). Brand-new deployments need none of this.

---

## Highlights

v1.0.4 is a one-line-per-role IAM hotfix that restores **AgentCore Memory** functionality. Both the App API task role and the AgentCore Runtime execution role granted every memory data-plane action *except* `bedrock-agentcore:GetMemory` — the action the SDK's `get_memory_strategies()` call requires to resolve a memory's strategy IDs. Without it, strategy discovery failed silently: the **Settings → memories/preferences page came back empty** (the App API returned empty lists with a 200), and the **agent stopped recalling long-term memories** (the runtime kept writing conversation events but ran with retrieval disabled). Granting `GetMemory` on both roles fixes both symptoms. This is an **infrastructure (IAM) change**, so it deploys via the platform (CDK) pipeline.

## 🐛 Bug fixes

- **Memory dashboard showed no memories/preferences.** `GET /memory` calls `get_memory_strategies()` → `bedrock-agentcore:GetMemory`, which the App API task role didn't allow. The call `AccessDenied`, strategy discovery returned no IDs, and the endpoint returned empty `facts`/`preferences` with a 200 — so the page rendered blank even though records existed. (`app-api-iam-grants.ts`)
- **Agent didn't recall long-term memories.** At session creation the runtime's `_discover_strategy_ids()` makes the same `GetMemory` call to build its retrieval namespaces; the runtime execution role also lacked the action, so retrieval was silently disabled ("long-term memory retrieval disabled") while event writes (`CreateEvent`) continued to succeed. (`inference-api-iam-roles.ts`)

## 🏗️ Infrastructure

- Added `bedrock-agentcore:GetMemory` to the `AgentCoreMemoryAccess` policy statement on **both** roles — the App API Fargate task role (scoped to the memory ARN) and the AgentCore Runtime execution role (scoped to `memory/*`). No other action names changed. Verified against the AWS Service Authorization Reference (`GetMemory` is a Read action on the `memory` resource type).

## 🚀 Deployment notes

This is an IAM change on `PlatformStack`, so it ships through the **platform (CDK)** pipeline, not the API-only backend path. After deploy, no data migration is needed and existing memories become visible immediately. Note the App API caches strategy discovery per process (`functools.lru_cache`) — a normal deploy rolls the ECS tasks and AgentCore Runtime, so the cache starts fresh; no manual restart required.

---

# Release Notes — v1.0.3

**Release Date:** June 30, 2026
**Previous Release:** v1.0.2 (June 29, 2026)

---

> ⚠️ **Upgrading from a beta?** 1.0.3 is an in-place upgrade from 1.0.0/1.0.1/1.0.2 with no migration. Moving from any pre-1.0.0 beta is still the destructive backup → teardown → redeploy → restore migration described in the [1.0.0 notes](#upgrading-an-existing-deployment). Brand-new deployments need none of this.

---

## Highlights

v1.0.3 is a maintenance patch — no application code or user-facing behavior changes. It's almost entirely **CI/CD pipeline work**: platform and backend deploys are now serialized through a shared concurrency group so they can't race each other onto the same ECS service / AgentCore Runtime / Lambda, push-triggered (path-scoped) auto-deploys are back on for the platform/backend/frontend workflows, and the duplicated test gates are consolidated into one reusable workflow. Rounding it out is a small dependency + CodeQL sweep (a `joserfc` CVE patch and a couple of unused-import removals). Operators on 1.0.x upgrade in place with no migration.

## 🔒 Security & 📦 Dependencies

- `joserfc` 1.6.3 → 1.7.2 (backend) and 1.6.5 → 1.7.2 (backup-data tooling), remediating Dependabot GHSA-wphv-vfrh-23q5 / CVE-2026-48990. (#526)
- Removed unused imports flagged by CodeQL — `Optional` in `agents/main_agent/agent_types.py`, `ssm` in `app-api/app-api-environment.ts`. (#526)

## 🔧 CI/CD

- **Serialized deploys.** `platform.yml` and `backend.yml` now share one repo-global concurrency group (`deploy-<ref>`), so a CloudFormation deploy and the API-driven backend code deploys queue instead of running at once and stomping the same ECS service / AgentCore Runtime / Lambda. Frontend stays independent; `cancel-in-progress` stays false. (#525)
- **Auto-deploy restored.** Push-triggered, path-scoped deploys are re-enabled for platform/backend/frontend (develop → development, main → production) after being manual-dispatch-only since v1.0.0. Each trigger is scoped to its own surface so unrelated changes don't redeploy. (#524)
- **Reusable test gates.** Duplicated test jobs are extracted into a shared `tests.yml` consumed by `ci`, `platform`, `backend`, `frontend-deploy`, and `nightly-deploy-pipeline`; skipped single-suite callers now render correct job labels instead of raw `${{ }}` expressions. (#524, #526)
- **Pipeline cleanup.** Pruned dead nightly tracks (AI coverage analysis, merge-validation) and orphaned scripts; `docs-deploy` now publishes from `main` (was `develop`), and `docs-deploy`/`release` are fork-gated so forks syncing `main` don't auto-publish or auto-release. (#524)

## 🚀 Deployment notes

In-place patch on the single-stack `PlatformStack` — no new infrastructure, env vars, or migration. The only operator-visible change is to CI/CD behavior: pushes to `develop`/`main` once again auto-deploy (path-scoped), and platform vs. backend deploys now queue rather than run concurrently.

---

# Release Notes — v1.0.2

**Release Date:** June 29, 2026
**Previous Release:** v1.0.1 (June 26, 2026)

---

> ⚠️ **Coming from a pre-1.0.0 (beta) deployment? Read the 1.0.0 release notes first.** There is **no special upgrade path for 1.0.2 itself** — if you're already on 1.0.0 or 1.0.1 you upgrade in place with no migration. But 1.0.0 was the single-stack consolidation, and upgrading **from any beta** to 1.0.0 (and therefore to 1.0.2) is a **destructive backup → teardown → redeploy → restore migration**, not an in-place `cdk deploy`. If you haven't already worked through it, do that before deploying 1.0.2: see [**Upgrading an existing deployment** (1.0.0 notes)](#upgrading-an-existing-deployment) below, or the published guide at <https://boise-state-development.github.io/agentcore-public-stack/deployment/upgrade/>. **Brand-new deployments need none of this.**

---

## Highlights

v1.0.2 is a small, security-focused patch on the 1.0.0 single-stack architecture with one notable behavior change. The headline is that **assistants can use tools again**: 1.0.0 had locked assistant chats to a knowledge-base-only, tool-free mode (#382), and this release reverts that so an assistant can once more leverage the user's selected MCP and built-in tools. Alongside it, this release lands a **CodeQL security-hardening sweep** (two HIGH findings around URL/host validation, a log-injection pass across 24 call sites, and a hardened CI checkout), remediates **6 Dependabot alerts** (Astro XSS/SSRF, esbuild dev-server file read, pydantic-settings path traversal), and fixes the **nightly coverage pipeline** that broke when the single-stack refactor moved its scripts. There is **no migration** — operators on 1.0.0 or 1.0.1 upgrade in place.

---

## Assistants can use tools again

1.0.0 introduced a deliberate restriction: assistant ("RAG") chats ran knowledge-base-grounded with **zero external tools** — the inference API forced `enabled_tools=[]` on assistant turns and the system prompt told the model it had no external tools (#382). That made assistants safe and predictable but also meant they couldn't search the web, hit an MCP server, or run code even when the user had those tools enabled. v1.0.2 reverts the restriction so assistants behave like a normal chat with the assistant's knowledge and instructions layered on top.

What stays the same: knowledge-base context is still pre-stuffed into the user message, and the assistant's custom instructions still apply. What changes: the user's tool-picker selection now flows through to the agent on assistant turns, and assistant chats once again emit tool-use and MCP-App events.

### Backend

- `inference_api/chat/routes.py` — dropped the `enabled_tools=[]` override in the `rag_assistant_id` branch so the client's tool selection reaches the agent, and removed the "Knowledge Base Grounding / no external tools" directive from both the with-instructions and no-instructions system-prompt paths (restoring the pre-#382 prompt composition).

### Frontend

- `chat-request.service.ts` — no longer force-sends `enabled_tools=[]` on assistant turns; the user's tool-picker selection rides along (skills mode stays gated on non-assistant turns).
- `preview-chat.service.ts` — the editor preview now forwards the owner's enabled tools instead of `[]`, and builds the streaming assistant message as ordered content blocks (text interleaved with tool use) wired to `onToolUse`/`onToolResult`, so the shared message-list renders tool cards in the preview exactly like a consumer chat.

### Test coverage

Specs updated to assert tools are forwarded on both assistant and preview turns (`chat-request.service.spec.ts`, `preview-chat.service.spec.ts`).

## 🐛 Bug fixes

- **Nightly coverage pipeline was failing.** The single-stack refactor removed `scripts/stack-app-api/` and `scripts/stack-frontend/`, but `nightly.yml`'s `test-backend`, `test-frontend`, and `install-frontend` jobs still called them, so they died with "No such file or directory." The three scripts were ported to the sanctioned post-refactor layout — `scripts/backend/test.sh`, `scripts/frontend/install.sh`, and `scripts/frontend/test.sh` — with behavior preserved 1:1 (uv install/sync + `pytest --cov`; `npm ci` for frontend and infra; `ng test --no-watch --coverage`). (#518)

## 🔒 Security

This release closes a CodeQL sweep (#521) and 6 Dependabot alerts (#520), each with regression tests where applicable.

**CodeQL code findings (#521):**

- **HIGH `py/incomplete-url-substring-sanitization`** — `external_mcp_client` previously substring-checked the whole URL for an AWS marker before deciding to SigV4-sign. A crafted URL with the marker in a path, query, or userinfo segment could trick it into attaching IAM credentials to a non-AWS host. It now parses the host with `urlparse` and matches an **anchored suffix**. Covered by `TestAwsUrlHostSanitization` (adversarial URLs).
- **HIGH `js/regex/missing-regexp-anchor`** — `admin-tool.model` now parses the host via `new URL` and **anchors** the AWS-endpoint regexes (`$`) so a spoofed host can't satisfy the match. Covered by `admin-tool.model.spec.ts` (13 cases including spoofed hosts).
- **MEDIUM `py/log-injection`** (24 sites across 16 files) — a new `apis.shared.security.scrub_log()` helper neutralizes CR/LF and control characters; every flagged user-controlled log value is now wrapped. Covered by `test_log_sanitize.py`.
- **MEDIUM `actions/untrusted-checkout`** — all `inputs.ref` checkouts in `nightly-deploy-pipeline.yml` now set `persist-credentials: false`.
- **WARNING `py/regex/duplicate-in-character-class`** — removed a stray `[` from a `re.VERBOSE` comment that the regex parser misread as a character class.

**Dependency CVE remediation (#520):**

| Component | Package | From | To | Fix |
|---|---|---|---|---|
| docs-site | `astro` | 6.3.1 | 6.4.8 | Reflected XSS via slot name, host-header SSRF in prerendered error page, spread-attribute XSS |
| docs-site | `esbuild` | — | 0.28.1 (override) | Dev-server arbitrary file read (GHSA-g7r4-m6w7-qqqr) |
| frontend | `esbuild` | — | 0.28.1 (override) | Dev-server arbitrary file read (transitive via `@angular/build` 21.2.16) |
| backend | `pydantic-settings` | 2.13.1 | 2.14.2 | `NestedSecretsSettingsSource` symlink traversal / local file read (GHSA-4xgf-cpjx-pc3j) |

## 🚀 Deployment notes

v1.0.2 is a patch on the single-stack `PlatformStack` architecture. Operators on 1.0.0 or 1.0.1 upgrade in place — **no migration, no new infrastructure, no new env vars.**

- **Behavior change to be aware of:** after deploying, assistant chats will once again use whatever tools the user has enabled (web search, MCP servers, code interpreter, etc.) rather than running knowledge-base-only. If your deployment relied on assistants being tool-free, note that this 1.0.0 restriction has been intentionally reverted.
- The security and dependency fixes require no operator action beyond deploying the new images/SPA build.

---

# Release Notes — v1.0.1

**Release Date:** June 26, 2026
**Previous Release:** v1.0.0 (June 24, 2026)

---

> ⚠️ **Coming from a pre-1.0.0 (beta) deployment? Read the 1.0.0 release notes first.** 1.0.1 lands just two days after 1.0.0, so most operators haven't deployed 1.0.0 yet. There is **no special upgrade path for 1.0.1 itself** — if you're already on 1.0.0 you upgrade in place with no migration. But 1.0.0 was the single-stack consolidation, and upgrading **from any beta** to 1.0.0 (and therefore to 1.0.1) is a **destructive backup → teardown → redeploy → restore migration**, not an in-place `cdk deploy`. If you haven't already worked through it, do that before deploying 1.0.1: see [**Upgrading an existing deployment** (1.0.0 notes)](#upgrading-an-existing-deployment) below, or the published guide at <https://boise-state-development.github.io/agentcore-public-stack/deployment/upgrade/>. **Brand-new deployments need none of this.**

---

## Highlights

v1.0.1 is the first patch on top of the 1.0.0 general-availability release, and it ships two operator- and user-facing additions. **Save conversations to connected apps** ("Save to…") extends the existing connector/adapter pattern in the write direction: a user can push a full conversation transcript out to an app they've connected — Google Drive is the reference destination — as a native Google Doc, Markdown, or plain-text file, reusing the same OAuth consent, RBAC visibility, and AgentCore Identity token flow as document import. **External (cross-account) Route53 hosted zones** lets deployments whose DNS zone lives in a different AWS account (or is managed out-of-band) stand up the full platform without the deploy failing on an in-account hosted-zone lookup. No action is required for existing 1.0.0 deployments; both features are additive.

---

## Save conversations to connected apps ("Save to…")

The platform already lets a user connect an app and pull documents **into** an assistant's knowledge base. v1.0.1 adds the opposite direction: save a conversation transcript **out** to a connected app. The architectural move is to split the existing connector pattern into a direction-agnostic **auth layer** (`OAuthProvider` + AgentCore Identity + consent UX, reused as-is) and a direction-specific **capability layer** — a new `ExportTargetAdapter` registry mirroring the read-side `FileSourceAdapter` registry. Google Drive is the first export target; adding OneDrive / SharePoint / Dropbox later is "write one adapter class, register it, and have an admin map a connector." (#507, #508, #509, #510, #511)

### Backend

- New `apis.app_api.export_targets` package: an `ExportTargetAdapter` contract (`adapter.py`), a code-shipped `registry`, the reference `adapters/google_drive.py` (creates a Drive file via the user's own token), a `render.py` transcript renderer (native Google Doc / Markdown / plain text), `models.py` (`ExportFormat`, `ExportInclude`, `ExportTargetError`), and `service.py` (connector resolution, RBAC visibility gate, AgentCore Identity token mint with consent/`503` handling).
- User-facing routes (`export_targets/routes.py`, on **app-api** — the inference-API boundary only proxies `/invocations` + `/ping`): `GET /export-targets` returns the catalog the "Save to…" dialog reads (per-connector `connected` state, `supportedFormats`, and a `browsable` flag for the folder picker), and `POST /sessions/{id}/export` renders the full transcript (paged, with a runaway guard) and creates the document via the resolved adapter. A `409` signals the user must complete OAuth consent; a `503` signals workload/callback misconfiguration.
- Admin mapping: new `GET /admin/export-target-adapters` plus an `OAuthProvider.export_target_adapter_id` field — a connector becomes an export target only once an admin maps it to a shipped adapter. Export receipts are persisted to session metadata (`ExportReceipt` on `sessions/models.py`, written best-effort by `add_export_receipt`) so the SPA can reflect "saved" state.

### Frontend

- New `ExportDialogComponent` (the "Save to…" dialog: connector picker, format picker driven by `supportedFormats`, optional destination-folder picker reusing the file-source browse dialog) and an `ExportService` (`session/services/export/`). A "Save to…" action is added to the conversation list (`session-list`). Admin connector form gains an export-target-adapter mapping dropdown.

### Test coverage

1,400+ lines of new tests: `test_export_routes.py`, `test_export_target_service.py`, `test_export_google_drive.py`, `test_export_render.py`, and `test_export_target_adapters_admin.py` on the backend; `export-dialog.component.spec.ts` and `export.service.spec.ts` on the frontend.

## External (cross-account) Route53 hosted zones

Deployments where the Route53 hosted zone for `domainName` lives in a **different AWS account** — or is otherwise managed out-of-band — previously failed: the stack's in-account `HostedZone.fromLookup` + ALIAS/A record creation cannot reach a zone it doesn't own. v1.0.1 makes DNS record management optional so these deployments succeed, with the platform emitting the records an operator needs to create by hand. (#512)

### Infrastructure

- New `manageDnsRecords` config flag (env `CDK_MANAGE_DNS_RECORDS`, context `manageDnsRecords`; **defaults to `true`**, so existing single-account deployments are unaffected). Loaded in `infrastructure/lib/config.ts` and threaded into the four custom-domain origins.
- When `manageDnsRecords=false`, the SPA, ALB, artifacts, and mcp-sandbox constructs still attach the custom domain + ACM certificate to each origin but **skip** the in-account `HostedZone.fromLookup` and ALIAS/A record creation. Instead each origin emits `CfnOutput`s with the **record name** and **alias target** (e.g. `AlbDnsRecordName`/`AlbDnsAliasTarget`, `FrontendDnsRecordName`, `ArtifactsDnsRecordName`/`ArtifactsDnsAliasTarget`, `McpSandboxDnsRecordName`/`McpSandboxDnsAliasTarget`) so an operator can create the records manually in the owning account.

### CI/CD

- `CDK_MANAGE_DNS_RECORDS` plumbed end-to-end: exported and passed as a `--context` flag in `scripts/common/load-env.sh`, and added to the job-level `env:` of the `platform.yml`, `teardown.yml`, and `nightly-deploy-pipeline.yml` workflows. The nightly smoke test reads it as well.

### Docs

- Deployment docs updated for the cross-account workflow — `docs-site` (environments, platform-cdk, troubleshooting) and `.github/docs/deploy/` (GitHub config, troubleshooting) plus `.github/ACTIONS-REFERENCE.md`.

## 🚀 Deployment notes

v1.0.1 is a patch release on the single-stack `PlatformStack` architecture introduced in 1.0.0. Operators already on 1.0.0 upgrade in place — there is **no migration**. Both features are additive and off by default until configured.

- **Save to… (export targets):** opt-in. An admin maps an existing connector (e.g. the Google Drive connector) to the `google-drive` export-target adapter from the admin connector form; until a connector is mapped, the "Save to…" dialog shows no destinations. No new infrastructure or env vars.
- **Cross-account DNS:** if your hosted zone is in the **same** AWS account as the deployment (the default), no action is required — `manageDnsRecords` defaults to `true` and behavior is unchanged. If your zone lives in a **different** account (or you manage DNS out-of-band), set `CDK_MANAGE_DNS_RECORDS=false` (GitHub Actions Variable), then after the deploy read the `*DnsRecordName` / `*DnsAliasTarget` CloudFormation outputs and create the matching ALIAS/A records in the owning account for the SPA, ALB, artifacts, and mcp-sandbox origins.

---

# Release Notes — v1.0.0

**Release Date:** June 24, 2026
**Previous Release:** v1.0.0-beta.27 (May 20, 2026)

---

## Highlights

**This is 1.0.0 — the first general-availability release.** After 27 betas, the platform is stable, and the headline of this release is as much about the *foundation* as the features built on it: the entire CDK app collapses from nine CloudFormation stacks into a single `PlatformStack` with a platform-as-bootstrap code-deploy model, so day-to-day code changes ship in ~2 minutes via AWS APIs and `cdk deploy` runs only when infrastructure actually changes.

On top of that foundation, 1.0.0 lands a large slate of product work: **Conversation Modes** (admin-curated system prompts users opt into), **file-source connectors and website crawling** that turn external systems and the open web into assistant knowledge bases, **self-service AgentCore Gateway MCP targets**, a **curated model catalog** with a new **Amazon Bedrock Mantle** provider, **per-turn context attribution**, and a public **Starlight documentation site**. It also delivers a complete **backup/restore disaster-recovery toolchain**, a coordinated **security-hardening sweep**, and remediation of **all 22 HIGH Dependabot findings**.

**Action required for operators with an existing deployment.** Because 1.0.0 consolidates the old nine-stack architecture into a single `PlatformStack`, upgrading any prior (beta) deployment is a **destructive backup → teardown → redeploy → restore migration** — not an in-place `cdk deploy`. We've written step-by-step instructions to make it as painless as possible. **Do not deploy over an existing environment without reading the [Upgrading an existing deployment](#upgrading-an-existing-deployment) section below first.** Brand-new deployments need no special steps.

---

## Upgrading an existing deployment

> **Read this before you deploy 1.0.0 over any existing environment.**

1.0.0 replaces the previous nine-stack CloudFormation layout with a single `PlatformStack`. There is **no in-place upgrade path** from a beta deployment — the old stacks must be torn down and replaced. Your data is preserved through a backup/restore cycle, but the steps are **destructive and must run in order**. We've written and tested detailed, click-by-click instructions so you can work through it confidently.

**Start here — the full step-by-step guides:**

- 📖 **[Upgrading from Multi-Stack](https://boise-state-development.github.io/agentcore-public-stack/deployment/upgrade/)** (published docs site) — the complete walkthrough with screenshots-level detail, a timeline, rollback steps, and migration gotchas.
- 📄 In-repo copy: [`.github/docs/deploy/upgrade-from-multi-stack.md`](.github/docs/deploy/upgrade-from-multi-stack.md)

**The migration at a glance** (≈45–75 min total — see the guide for the exact inputs for each workflow):

1. **Back up** — run the **Backup Data (Pre-Migration)** workflow. This is the most critical step; confirm `summary.failed` is zero and note the `{prefix}-backup-{timestamp}` bucket name. Do not proceed on a failed backup.
2. **Tear down** — run the **Teardown All Infrastructure** workflow (`confirm: DESTROY`) to delete the old stacks. If your environment retained stateful resources (`CDK_RETAIN_DATA_ON_DELETE=true`), clear them — at minimum delete the legacy `/{prefix}/{app-api,inference-api}/image-tag` SSM parameters, which otherwise fail the first new deploy.
3. **Redeploy** — run **Platform Stack** → **Backend Deploy** → **Frontend Deploy** → **Seed Bootstrap Data**.
4. **Restore** — run the **Restore Data** workflow against your backup bucket with `dry_run: true` first, then `dry_run: false`.
5. **Verify** — confirm login, chat history, file uploads, the admin dashboard, and RAG assistants.

> ⚠️ **Two things to know going in:** the backup bucket is immutable and survives every teardown (it's your safety net and rollback source), and **Cognito passwords do not transfer** — native-password users must use "Forgot Password" on first login, while federated (OIDC/SAML) users are unaffected.

Brand-new deployments skip all of this — see [Deployment notes](#-deployment-notes).

---

## Single-stack platform-as-bootstrap architecture

The biggest structural change in the project's history: the CDK app that used to be nine CloudFormation stacks is now one `PlatformStack`.

**Why this overhaul.** The multi-stack layout treated the platform like a fleet of independently deployable microservices — but the application is, by definition, a **monolith**: one cohesive product whose pieces are released together, version-locked, and only ever deployed as a unit. Splitting it across nine stacks bought none of the benefits of microservices and all of their operational cost. Cross-stack `Fn::ImportValue` references created brittle deploy-ordering requirements; a change in one stack routinely forced careful, manual sequencing of the others; and the seams between stacks were a constant source of deployment issues and gotchas — exported-value locks that blocked updates, drift between stacks that had to be reconciled by hand, and first-deploy chicken-and-egg problems. Consolidating into a single `PlatformStack` removes that entire class of failure: there are no cross-stack references to order, no inter-stack drift to reconcile, and one `cdk deploy` either succeeds or rolls back as a whole. Treating the monolith as a monolith from a DevOps standpoint is simpler to reason about, faster to deploy, and dramatically less error-prone.

### Infrastructure

- `infrastructure/lib/platform-stack.ts` composes ~39 single-responsibility constructs under `lib/constructs/` (network, identity, data, rag, artifacts, mcp-sandbox, agentcore, inference-api, app-api, fine-tuning, spa, zones). It is built in two phases — the constructor (data + edge + Cognito + AgentCore Memory/Code-Interpreter/Browser/Gateway) and `wireCompute()` (Inference Runtime + SageMaker + App API Fargate) — which eliminates every cross-stack `Fn::ImportValue` and all deploy-ordering between stacks. `npx cdk list` now returns exactly `${prefix}-PlatformStack`.
- **Platform-as-bootstrap.** CDK ships small, byte-stable placeholder assets from `infrastructure/bootstrap-assets/{app-api,inference-api,rag-ingestion,artifact-render}/` (stdlib HTTP servers / 503 handlers). The real code ships out-of-band via AWS control-plane APIs: `aws ecs register-task-definition` + `update-service` (app-api Fargate), `aws bedrock-agentcore-control update-agent-runtime` (inference-api Runtime), and `aws lambda update-function-code` (rag-ingestion image Lambda + artifact-render zip Lambda). Because CFN tracks each `Code`/`image` property from its own constant model, subsequent Platform deploys leave the out-of-band-deployed real code untouched.
- All per-component CDK feature flags were removed (`CDK_FRONTEND_ENABLED`, `CDK_APP_API_ENABLED`, `CDK_INFERENCE_API_ENABLED`, `CDK_GATEWAY_ENABLED`, `CDK_FILE_UPLOAD_ENABLED`, `CDK_ASSISTANTS_ENABLED`, `CDK_RAG_ENABLED`, `CDK_FINE_TUNING_ENABLED`, `CDK_ARTIFACTS_ENABLED`, `CDK_MCP_SANDBOX_ENABLED`). The platform now deploys everything, always.

### CI/CD

- New `platform.yml` (CDK), `backend.yml` (build → API-driven code deploy), and `frontend-deploy.yml` workflows replace the legacy per-stack workflows, which were deleted along with their scripts and tests. A content-hash Docker build pipeline under `scripts/build/` skips a rebuild when the computed hash already exists as an ECR tag. Day-to-day backend code deploys in ~2 minutes without touching CloudFormation; `cdk deploy` runs only on real infrastructure changes.

### Test coverage

Carried forward from the refactor's stabilization: 7 policy-level assertions in `infrastructure/test/security-policy.test.ts` (Action:\* + Resource:\* prohibition, BFF-cookie-key Decrypt-only, every bucket SSE + public-access-block + `enforceSSL`, every DDB table SSE), 5 in `compute-image-resolution.test.ts` (SSM-resolved image shape), 2 in `ssm-safety.test.ts` (same-stack `valueForStringParameter` deadlock at synth), and a `tests/supply_chain/test_env_var_contract.py` reflection test that fails on any orphan CDK env var.

---

## Conversation Modes

**Shipped enabled.** Admins curate a catalog of custom system prompts ("Guided Learning", "Concise", and so on) that users opt into per conversation.

### Backend

- New `apis.shared.system_prompts` module (models / repository / service) with optimistic-concurrency updates so a concurrent delete+edit can't resurrect a deleted prompt. Admin CRUD `/admin/system-prompts` (full `prompt_text`) and a user read `/system-prompts` (name + description only — prompt text stays server-side). Inference resolves the active prompt via `chat/system_prompt_resolver.py` and appends it to the base prompt; gating skips resume, continuation, preview, and assistant-attached turns. Selection precedence is request-body-first (so the first turn of a new session works without a metadata round-trip), with session preferences as fallback.

### Infrastructure

- New `SystemPromptsTable` DynamoDB construct (env `DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME`; app-api CRUD, inference-api `GetItem` only); name + ARN published to SSM.

### Frontend

- Lazy `SystemPromptsService`, admin list/form pages, and a per-conversation chip + radio group in the settings panel.

---

## Knowledge bases: file-source connectors and website crawling

Two complementary ways to fill an assistant's knowledge base from outside a manual upload.

### File-source connectors

A four-PR arc turns OAuth connectors into RAG document sources. A provider-agnostic backend (`FileSourceAdapter` ABC + shipped-code-only registry, normalized `FileEntry`/`BrowseResult`/`SourceRoot`/`DownloadedFile` contract) ships with a `GoogleDriveAdapter` (Drive v3 browse/search/download including native-doc export). The `Document` model gains provenance (`sourceConnectorId`/`sourceAdapterKey`/`sourceFileId`/`sourceEtag`/`importedByUserId`). Admins opt a connector in by mapping it to an adapter (`OAuthProvider.file_source_adapter_id`, validated against `compatible_provider_types`); users browse via `GET /file-sources`, `GET /connectors/{id}/roots|browse|search`, and import via `POST /assistants/{id}/documents/import` (202), which creates provenance-bearing `Document` rows then stages downloads to the documents S3 bucket where the existing ingestion Lambda chunks and embeds them. The SPA adds a `FileSourceBrowserDialogComponent` (CDK modal). Two correctness fixes followed: sending the `OAuth2CallbackUrl` header (#373) and consent-matched `customParameters` (#374).

### Website crawling

A new `apis/app_api/web_sources/` package adds an "Add web content" flow (`POST /assistants/{id}/web-sources/crawl` + crawl-status endpoints). The bounded-BFS crawler is robots.txt-respecting, same-domain, SSRF-guarded, with per-host jitter, bounded concurrency, a 5 MB/page cap, and a 15-minute budget; extraction is trafilatura→markdown (BeautifulSoup fallback) written to the documents bucket for the existing S3-event ingestion. `CrawlJob` rows persist in the existing assistants table via the adjacency-list pattern with a 30-day TTL on terminal rows and a self-heal that auto-finalizes stuck `running` rows. The SPA adds a `WebSourceDialogComponent` with depth/max-pages/concurrency sliders and a 5s active-crawl poller that merges discovered pages incrementally. New deps: `beautifulsoup4` 4.13.5, `trafilatura` 2.0.0.

---

## Assistants: collaboration and editor UX

- **Viewer/editor share permissions.** Per-user permission levels on shared assistants: `AssistantSharesResponse.sharedWith` becomes `ShareEntry[]`, a `PATCH /assistants/{id}/shares` endpoint lands, and editors can edit settings/docs/test-chat but cannot delete, change visibility, or manage shares — gated across the assistants/documents/inference routes (no new table). The UI adds per-row "Can view / Can edit" selects, "Editor" badges, and an owner-only Share button.
- **Knowledge-base grounding.** Consumer chat with an assistant (`rag_assistant_id`) now runs with **zero external tools**, grounded in the knowledge base only — enforced at the inference-API chokepoint (`enabled_tools=[]`) plus a "## Knowledge Base Grounding" system-prompt section.
- **Editor redesign.** The editor adopts the `rounded-2xl` list/form language; connectors surface as buttons above the drop zone (opening the browser dialog targeted at that connector), the three "add knowledge" groups collapse into a single inline action row with skeleton chips, OAuth consent starts in place from the connector button, `complete` documents are downloadable, and the preview hides voice/settings while exposing file attachments via `file_upload_ids`.

---

## Gateway MCP self-service targets

Admins can register an externally deployed MCP server as a target on the shared AgentCore Gateway directly from the admin Tools form — no infrastructure change. A `MCPGatewayConfig` model (listing-mode / credential-type / grant-type enums mirroring `bedrock-agentcore-control`, per-tool `MCPToolEntry` flags, AWS-assigned `target_id`/`gateway_arn`) is serialized under `mcpGatewayConfig`; a `GatewayTargetService` drives the lifecycle (create-AWS-first, update-reconcile, hard/soft delete with 409/502 mapping) and `GET /admin/tools/{tool_id}/gateway-status`. The form supports Discover-from-server and OAuth co-gating, a new `NONE` (public-endpoint) credential type as the default, correct `iamCredentialProvider{service,region?}` for `GATEWAY_IAM_ROLE` targets, and a per-target `lambda:InvokeFunctionUrl` grant/revoke (`gateway_lambda_grant.py`) that replaces the prior standing `mcp-*` wildcard. A shared `gateway_identity.resolve_gateway_id` unifies how the agent and the service resolve the gateway — fixing a bug where the agent read a different hardcoded gateway than the admin form wrote to — and the runtime expands catalog tools to `gateway_<target>___<tool>` ids.

### Infrastructure

- `AgentCoreGatewayConstruct` publishes `/{prefix}/gateway/id` to SSM (read at runtime, never at CFN deploy time). app-api gains `ssm:GetParameter` on it plus `bedrock-agentcore:{Create,Get,Update,Delete,List}GatewayTarget` scoped to `gateway/*`.

---

## Curated model catalog and the Amazon Bedrock Mantle provider

Model administration moves from hand-entry to a curated catalog: `model-catalog.page.ts` + `models/curated-models.ts` define fully-specified Bedrock entries (Claude Haiku/Sonnet/Opus 4.x) with pricing, modalities, and per-param specs; an add dialog collects role IDs before POST while "Preview & customize" hands a template to the model form; each card shows a light/dark provider logo. A same-session follow-up fixed the float-`thinking.default` validation bug that ghosted stored models from the list, and added a delete-confirmation modal and loading state.

Separately, **Amazon Bedrock Mantle** is added as a first-class provider — AWS's OpenAI-compatible surface for open-weight models (qwen, gpt-oss, gemma, deepseek). A new `apis/shared/bedrock/bearer_token.py` mints a SigV4-presigned short-lived token so the OpenAI SDK can drive the Mantle endpoint, and `GET /admin/mantle/models` browses the live regional roster to seed the form.

---

## Per-turn context attribution

A four-PR foundation answers "what is filling the context window?". The AgentCore runtime role is granted `bedrock:CountTokens`; `model_config.py` sets `use_native_token_count=True` with an inference-profile-aware `core/bedrock_count_tokens.py` so Bedrock returns authoritative counts instead of the chars/4 heuristic. A `ContextAttributionHook` (on `BeforeModelCallEvent`) splits the count into system / tools / messages partitions, and the stream coordinator attaches it to the turn's final `metadata` SSE event as `contextBreakdown`. The SPA renders a "Context: <total>" pill, modeled as an open-ended partition list so future partition splits are additive and non-breaking, gated behind the existing show-token-count setting.

---

## MCP Apps host-renderer

Building on the beta.27 foundation, this release made the host-renderer production-solid.

- **Progressive rendering (SEP-1865).** The App frame mounts early at the tool's `content_block_start` and streams `ui/notifications/tool-input-partial`, so Apps that animate from streaming arguments (e.g. Excalidraw camera tours) work end-to-end (`integrations/mcp_apps.py`, `streaming/stream_coordinator.py`, `apis/shared/mcp_apps/partial_json.py`).
- **Refresh survival.** Model-initiated UI resources persist as gzipped HTML in the sessions-metadata table (`ui_resource_store.py`, SK `UIRES#<toolUseId>`, 90-day TTL, ownership re-check) and replay through the messages response.
- **Rendering and robustness fixes.** The 150px iframe collapse (CSSOM 100%-height chain), the fullscreen overlay stacking/sizing (`z-index:9999` fixed iframe; entry-animation transform no longer traps it), the `<meta>`-vs-header CSP mismatch that blocked `eval` Apps, spec-array `ui/message` content, transient-TLS retry on MCP client start, and a fullscreen title-bar with reachable consent.

---

## 🐛 Bug fixes

- **Managed-models list ghosting** — stored models with a whole-number float `thinking.default` (DynamoDB Decimal roundtrip) failed validation on read and were silently skipped from the list while create still saw them ("already exists" + invisible row). The validator now accepts whole-number floats; adds a delete-confirmation modal and loading state (#394).
- **File-upload duplicate-name misclassified as "file too large"** — narrowed the size classifier to require explicit size markers and added a dedicated duplicate-name branch (#403).
- **Gateway IAM targets rejected** — an HTTP-endpoint `mcpServer` target requires an explicit `iamCredentialProvider`; the agent Gateway client was also repointed from a hardcoded SSM param to the CDK `/{prefix}/gateway/id` so admin-registered targets reach the agent (#457).
- **arm64 image mismatch** — `rag-ingestion` was built amd64 against an arm64 Lambda (`Runtime.InvalidEntrypoint`, uploads stuck with no embeddings); now built on native ARM runners (#496).
- **Artifact-render drift** — re-deploys the render Lambda code when the live function drifts from what we shipped, so the CDK bootstrap 503 stub stops serving `artifacts.{domain}` (#438).
- **MCP-sandbox cert regression** — restored the deploy var lost in the stack consolidation (NXDOMAIN → App `postMessage` origin mismatch) with a synth-time guard (#434).

---

## 🔒 Security

A coordinated defense-in-depth pass, mostly as direct commits plus PRs #443/#458/#484. Its keystone is a new `backend/src/apis/shared/security/` package (#443):

- `url_validator.validate_external_url` — a DNS-rebinding-safe SSRF guard that rejects loopback, link-local (incl. 169.254 cloud-metadata), RFC1918/ULA, multicast, reserved, unspecified, and CGNAT targets, resolving every DNS answer before allowing a request.
- `ownership` helpers (`require_session_owner` / `require_memory_owner` / `require_file_owner`) whose handler maps `OwnershipError` → HTTP **404, not 403**, erasing the not-found-vs-forbidden enumeration oracle.
- AWS `ClientError` / validation handlers registered in both API apps that collapse upstream detail to generic 400/502/422 bodies.

Adopted across the surface: `fetch_url_content` runs every URL — including each manual redirect hop (`follow_redirects=False`, ≤3 hops) — through the validator; outbound MCP SigV4 signing only attaches task IAM credentials to recognized AWS hosts and refuses otherwise; Code Interpreter inputs from `generate_diagram_and_validate` and `analyze_spreadsheet` are walked by a static AST policy against a plotting/dataframe allowlist that bans subprocess/os/sys/socket/eval/exec/dunder access. Identity is pinned to the validated session, not request bodies (`POST /users/me/sync` derives email and roles from `current_user.*`); system prompts are wrapped in a non-escapable `PLATFORM_SAFETY_FLOOR`; session-metadata `PUT` rejects cross-owner ids; `jwt_role_mappings` are regex-validated with map-everyone tokens banned on `system_admin`; admin read paths were sanitized and CloudFront/ALB pinned to a TLS 1.2+ minimum baseline. Each item ships regression tests under `backend/tests/security/`, `tests/rbac/`, and `tests/routes/`.

---

## ⚡ Performance

- **Re-enabled Strands Bedrock auto prompt caching** — `ModelConfig.to_bedrock_config()` emits `CacheConfig(strategy="auto")` again, now safe after the upstream cachePoint/document-attachment collision was resolved in strands-agents 1.39.0 (#471).

---

## ⚠️ Breaking changes

These are breaking only for forks still on the legacy multi-stack layout. Fresh and single-stack deployments are unaffected.

- **Nine-stack → single `PlatformStack`.** Every legacy stack (Infrastructure / Frontend / AppApi / InferenceApi / Gateway / Artifacts / McpSandbox / RagIngestion / SageMakerFineTuning) is removed; `bin/infrastructure.ts` instantiates only `${prefix}-PlatformStack`. All per-component CDK feature flags were removed. Migration path: `.github/docs/deploy/upgrade-from-multi-stack.md` (legacy SSM cleanup + teardown of the old stacks). (#396)
- **SSM `image-tag` contract.** `/{prefix}/{app-api,inference-api,rag-ingestion}/image-tag` changed from a bare tag/short-SHA to a FULL ECR URI. A stale legacy value will fail the first `PlatformStack` deploy on CFN pattern-validation; the seed script auto-repairs it. (#420)
- **Assistant consumer chat runs tool-free.** Chatting with an assistant is now knowledge-base-grounded with `enabled_tools=[]`; a side effect is that MCP-App `ui_resource` events no longer fire for assistant chats. No migration needed. (#382)

---

## 🏗️ Infrastructure

- **Shared CloudFront wildcard cert.** New top-level `CDK_CLOUDFRONT_CERTIFICATE_ARN`; the SPA / artifacts / mcp-sandbox origins fall back to it (a section-specific cert still wins), so a single `us-east-1` `{domain}` + `*.{domain}` cert covers all edge origins, with cert-missing guards (#491).
- **New tables.** `system-prompts` DynamoDB table (Conversation Modes; app-api CRUD, inference-api `GetItem` only), with name + ARN published to SSM.
- **Restored SSM contracts.** ~22 parameters (17 table, 4 bucket, `/inference-api/memory-id`) that the consolidation dropped and the restore tooling reads were republished (#421).
- **Context attribution.** AgentCore runtime execution role granted `bedrock:CountTokens` (#428).

---

## 🔧 CI/CD improvements

- **Deploy workflows are `workflow_dispatch`-only for this release.** `platform.yml`, `backend.yml`, and `frontend-deploy.yml` no longer run on `push` — their push triggers are commented out so that forking or syncing the codebase never auto-deploys infrastructure or code into your AWS account. Deploy intentionally from the **Actions** tab. Re-enable later by uncommenting the `push:` block in each workflow.
- New `platform.yml`, `backend.yml`, and `frontend-deploy.yml` workflows; `nightly-deploy-pipeline` rewritten platform → backend → frontend; legacy per-stack workflows deleted (#396).
- New `ci.yml` pull-request test gate (backend pytest / frontend vitest / infra jest) on PRs into `develop`/`main`; deploys never run on PRs (#490).
- New `docs-deploy.yml` publishes the Starlight site to GitHub Pages (#432).
- `aws-cdk` CLI pinned 2.1128.0 + Node 22 pinned in deploy jobs (#492); `Backend Stack` renamed to `Backend Deploy` (#423); and the stale `6.` prefix was dropped from the Seed Bootstrap Data workflow.

### GitHub Actions upgrades

| Action / Tool | From | To |
|---|---|---|
| `aws-cdk` (CLI) | 2.1120.0 | 2.1128.0 |
| `aws-cdk-lib` | 2.251.0 | 2.260.0 |

---

## 📦 Dependency upgrades

Remediates all 22 HIGH Dependabot findings plus easy MEDIUM/LOW (the same set merged across #487, #488, #489).

### Backend

| Package | From | To |
|---|---|---|
| `cryptography` | 47.0.0 | 48.0.1 |
| `starlette` | 1.0.0 | 1.3.1 |
| `python-multipart` | 0.0.27 | 0.0.31 |
| `pyjwt[crypto]` | 2.12.1 | 2.13.0 |
| `urllib3` | (range) | pinned 2.7.0 |
| `aiohttp` | 3.13.5 | 3.14.1 |
| `authlib` | 1.7.0 | 1.7.1 |
| `idna` | (range) | pinned 3.15 |
| `beautifulsoup4` | — | 4.13.5 (new) |
| `trafilatura` | — | 2.0.0 (new) |

### Frontend

| Package | From | To |
|---|---|---|
| `@angular/*` | 21.2.11 | 21.2.17 |
| `@angular/cdk` | 21.2.9 | 21.2.14 |
| `@angular/build`, `@angular/cli` | 21.2.9 | 21.2.16 |
| `mermaid` | 11.14.0 | 11.15.0 |
| `hono` (override) | ≥4.12.14 | ≥4.12.25 |
| `undici` (override) | ≥7.25.0 | ≥7.28.0 |
| `vite` (override) | ≥7.3.2 | ≥8.0.16 |
| `piscina` (override) | — | ≥5.2.0 (new) |
| `@babel/core` (override) | — | bounded 7.29.7 |

### Infrastructure

| Package | From | To |
|---|---|---|
| `aws-cdk-lib` | 2.251.0 | 2.260.0 |
| `aws-cdk` (CLI) | 2.1120.0 | 2.1128.0 |

---

## 🚀 Deployment notes

- **Fresh deployments:** no special steps. Trigger each workflow from the **Actions** tab (deploys are manual `workflow_dispatch` this release): **Platform Stack** (CDK), then **Backend Deploy**, **Frontend Deploy**, and **Seed Bootstrap Data**.
- **Upgrading an existing deployment:** this is a destructive backup → teardown → redeploy → restore migration — see the [Upgrading an existing deployment](#upgrading-an-existing-deployment) section above for the full walkthrough and links. The `image-tag` SSM parameters must hold full ECR URIs (the seed step repairs stale legacy values).
- **New certificate option.** If you want one wildcard cert across all edge origins, set `CDK_CLOUDFRONT_CERTIFICATE_ARN` (must be in `us-east-1`); section-specific cert ARNs still take precedence.
- **Disaster recovery.** The `Backup Data (Pre-Migration)` and `Restore Data` workflows snapshot and replay all application data (DynamoDB, S3, S3 Vectors, Cognito) into a deployed `PlatformStack`; always run `Restore Data` with `dry_run: true` first.

---

# Release Notes — v1.0.0-beta.27

**Release Date:** May 20, 2026
**Previous Release:** v1.0.0-beta.26 (May 13, 2026)

---

## Highlights

The largest release since the BFF cutover. Beta.27 lands two new user-visible surfaces, both built on top of brand-new CDK stacks, plus a major admin redesign and a handful of inference-API correctness fixes.

- **Artifacts** — the agent can now produce versioned, iframe-isolated HTML, Markdown, and code artifacts that render in a docked side panel beside the chat. Backed by a new `ArtifactsStack` (S3 + DynamoDB + render Lambda + CloudFront on `artifacts.{domain}`) and short-lived JWT render tokens minted by app-api.
- **MCP Apps host renderer** — third-party MCP servers can ship UI alongside their tools. The agent advertises a UI extension on `initialize`, fetches `ui_resource` payloads via `resources/read`, and the SPA frames them in a sandboxed `<mcp-app-frame>` over a strict CSP, with an app-initiated `tools/call` proxy and explicit user consent. Backed by a new `McpSandboxStack` (CloudFront origin on `mcp-sandbox.{domain}` with dynamic per-resource CSP via a CloudFront Function). Default-on this release.
- **Admin shell redesign** — the 15-card admin grid is replaced with a persistent grouped sidebar, and dense list redesigns for models and tools turn cards into compact expandable rows. Quotas and Fine-Tuning collapse from seven sibling routes into two tabbed pages.
- **Recoverable `max_tokens` truncation** — what used to be a leaky, infinite-looping `MaxTokensReachedException` is now an inline "Response length limit reached" notice with a Continue button that resumes the truncated turn instead of resending the prompt. Survives a page refresh.
- **Model-aware adaptive thinking** — Opus 4.7's 400 on `thinking.type=enabled` is fixed: Opus 4.6/4.7, Sonnet 4.6, and Mythos now emit `{type: adaptive, display: summarized}` and depth is governed by a new admin- and user-configurable `effort` knob. Older models keep the legacy `enabled` shape.
- **`/ping` reaper fix** — fixes silent mid-stream microVM reaping by emitting the integer `time_of_last_update` field AgentCore's idle reaper requires. Workaround for `bedrock-agentcore-sdk-python#471` until async-task busy tracking lands.
- **Pre-migration backup tool** — `scripts/backup-data/` produces a complete, restore-friendly snapshot of all DynamoDB tables, user-content S3 buckets, and Cognito (config + users + groups + IdPs + plaintext app-client secrets) for a given `CDK_PROJECT_PREFIX`. Workflow-dispatch wired.
- **Dependency upgrades** — `bedrock-agentcore` 1.6.4 → 1.9.1 (with coupled `boto3` 1.42.96 → 1.43.9) and `strands-agents` 1.39.0 → 1.40.0.

This release adds two new CDK stacks (`ArtifactsStack`, `McpSandboxStack`) and one new DynamoDB table (`user-menu-links`). Both new stacks are gated by config flags. Deploy order matters — see "Deployment notes" below.

---

## Artifacts

The agent can now author versioned standalone documents — HTML pages, charts, Markdown reports — that render in a sandboxed iframe alongside the chat. Artifacts solve two problems the existing `create_visualization` and Code Interpreter outputs couldn't: persistence (the user can re-open and download), and isolation (HTML/JS runs in a cross-origin sandbox so it can't read cookies or the SPA DOM).

### Architecture

A new leaf stack, `ArtifactsStack`, owns the rendering pipeline:

- **DynamoDB `user-artifacts` table** — version log + HEAD pointer per artifact. PK `USER#{user_id}`, SK `ARTIFACT#{aid}#V#{version:05d}` for versions and `ARTIFACT#{aid}#HEAD` for the latest pointer. GSI1 indexes by `SESSION#{session_id}` so the SPA can list artifacts produced in the current chat.
- **S3 `artifacts-content` bucket** — private, no CORS. Layout `{user_id}/{aid}/v{n}/index.html`. Versions are immutable: there's no `s3:DeleteObject` grant on the inference-api role, so an `update_artifact` writes a new version and re-points HEAD instead of mutating.
- **Render Lambda** — validates a render-token JWT scoped to one `(artifact_id, version)`, fetches the blob from S3, and returns it with a strict per-origin CSP that allows inline `<style>` / `<script>` plus scripts from `cdn.tailwindcss.com`, `esm.sh`, `cdn.jsdelivr.net`, and `unpkg.com`. `connect-src 'none'` — artifacts cannot make outbound network calls.
- **CloudFront distribution on `artifacts.{domain}`** — terminates TLS, attaches the security-headers policy. The artifact origin is intentionally a different cookie-jar host from the SPA so a script in an artifact can't read `__Host-bff_session`.
- **HMAC signing key** — the render-token signing secret lives in Secrets Manager in `InfrastructureStack` (not `ArtifactsStack`), so app-api and the render Lambda can both read it without `ArtifactsStack` becoming a stack-dependency root. App-api mints short-lived JWTs that the SPA embeds as the iframe `src`.

### Agent tools

Two new built-in tools, registered as default public tools so the feature is usable on first deploy without an admin opting them in per role:

- `create_artifact(title, content, content_type="text/html; charset=utf-8")` — writes v1. HTML mode requires a complete standalone document (`<!doctype html>` + full `<html>`); Markdown mode (`content_type="text/markdown"`) takes raw GFM and the writer wraps it in a self-contained HTML render harness server-side.
- `update_artifact(artifact_id, content, ...)` — writes a new version and re-points HEAD; the render-token mints against the latest version when the panel updates.

The system prompt documents the dual authoring contract and the CSP allowlist (Chart.js auto-registering build, `import Chart from "https://esm.sh/chart.js@4/auto"` etc.) so the model produces output that actually renders.

### SSE + SPA

A new `artifact` SSE event streams from the inference-api each time the agent creates or updates an artifact. The frontend has:

- `ArtifactStateService` + `ArtifactHttpService` + `ArtifactDownloadService` — signal-backed state, render-token fetch, blob download.
- A docked, resizable artifact panel beside the chat that auto-opens on first creation, shows a skeleton while loading, and on update jumps to the latest version. Per-version history cards in the panel let the user step backwards through revisions.
- An inline artifact card anchored to the producing message, with a preview/code toggle (syntax-highlighted source view) and a download button on both the card and the panel.
- Full-width inline cards, scoped `isolation: isolate` z-indexing so a focused artifact card doesn't escape its message row, and live tool-output streaming into the tool rail while the artifact is being authored.

### Configuration

Artifacts is opt-in at deploy time via `CDK_ARTIFACTS_ENABLED=true`. When enabled, `CDK_HOSTED_ZONE_DOMAIN` and `CDK_ARTIFACTS_CERTIFICATE_ARN` become required. Validation runs on every stack synth, so all five consumer GitHub workflows now thread these env vars through the OIDC composite action — a missing var on a non-`ArtifactsStack` workflow would otherwise fail synth.

---

## MCP Apps Host-Renderer

A scoping document landed early in the cycle (`docs/kaizen/scoping/mcp-apps-host-renderer.md`) and the implementation followed a deliberate seven-PR sequence (#339 PR #0 → #349 PR #7). The result: third-party MCP servers can ship a small interactive UI alongside their tools, and that UI renders in a sandboxed iframe with the same isolation guarantees as artifacts.

### Architecture

A new leaf stack, `McpSandboxStack`, mirrors the artifacts pattern:

- **CloudFront distribution on `mcp-sandbox.{domain}`** — fronts an S3 origin that serves a tiny "basic-host" mount page. App URLs land at `mcp-sandbox.{domain}/<resource-encoded-path>`, the mount page reads the encoded resource URL from the path and frames the actual MCP App content in an inner blob iframe with `allow-same-origin` matching the basic-host reference.
- **Dynamic per-resource CSP** — a CloudFront Function on the viewer-response decodes a `?csp=` query param (URL-encoded `frame-ancestors` source list scoped to that one resource) and emits a per-request `Content-Security-Policy` header. The function source is loaded from `assets/mcp-sandbox/csp-function.js` and the `frame-ancestors` allowlist is JSON-injected at synth — the substitution asserts the placeholder marker is present exactly once so a future refactor that loses it fails loudly at synth, not at edge runtime.
- **Outer `frame-ancestors` allowlist** — configurable via `mcpSandbox.extraFrameAncestors` so a deploy can permit framing from custom origins (preview environments, alternate SPA hosts) without rebuilding the function asset.

### MCP protocol surface

The agent now advertises an `experimental.ui` extension during MCP `initialize` so a server knows whether the host can render UI. Tools whose only output is a `ui_resource` are filtered out for non-capable clients (the existing API-key path, scripted callers).

When a tool result references a UI resource, the agent fetches it via the standard MCP `resources/read` flow and emits a `ui_resource` SSE event with `uri`, `permissions`, and a `sandboxOrigin` that points at the deployed `mcp-sandbox` host (sourced from SSM, so the value is correct per environment). Two app-initiated message types complete the protocol:

- `ui/message` — the App pushes structured data into the chat input as a tool-input draft (acts like a smart form).
- `ui/update-model-context` — the App contributes context the agent should consider on the next turn.
- `tools/call` proxy — the App can invoke other tools on the same MCP server. The frontend brokers these through app-api over an event broker rather than letting an iframe call the Bedrock runtime directly.

### Frontend

- `<mcp-app-frame>` Angular custom element + a `postMessage` bridge that enforces the allowed message types and rejects unknown origins.
- A consent prompt rendered as an inline message component — the user explicitly approves an App before it gets framed. Consent decisions persist across reloads via a card store.
- Reload persistence: the consent service hydrates from a card store on session load so a refresh doesn't re-prompt for a previously-approved App.
- A signal-backed `ToolRendererRegistryService` (the PR #0 refactor) keyed by tool name. The `mcp-app-frame` renderer is the first registry-aware tool result; the default renderer reproduces the prior text/JSON/image switch verbatim, so all existing tool-result cards render identically. `calculator`, `fetch_url_content`, and `create_visualization` were migrated as proof points to validate the registry shape.

### Default-on

`Defaults.MCP_APPS_HOST_ENABLED` flips `False → True` this release, and `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` is wired into the inference-api runtime env from the `mcp-sandbox` SSM origin (gated on `config.mcpSandbox.enabled`, mirrors the artifacts conditional-SSM pattern). Without that wiring a deployed environment would emit `ui_resource` events with an empty `sandboxOrigin` and the SPA couldn't frame the App. Two synth tests cover the present/absent paths.

A budget-allocator-server example is committed as a reference MCP App, and `step-04-deploy.md` / `step-05-verify.md` runbooks gain "Register an MCP-Apps-capable MCP server" sections plus a manual e2e dogfood scenario.

### CSP / isolation hardening (PRs #352–#360)

Several follow-ups landed during dogfood to align the host with the upstream `ext-apps` basic-host reference:

- Outer CSP + inner mount alignment with the reference implementation (#353).
- Blob-iframe rendering, first-class block element, Angular 21-specific fixes (#352).
- Sandbox CFN `Comment` shortened to fit the 128-char AWS cap, twice (#356, #357).
- URL-decoded `?csp=` parsing in the sandbox CFN (#358), with the `x-csp-debug` diagnostic header added during the investigation (#358) and removed once the fix landed (#359).
- Inner App iframe got `allow-same-origin` to match the basic-host reference (#360).

---

## Admin Shell Redesign

The 15-card admin grid had outgrown its container — a sibling navigation surface that grew unboundedly with every new admin domain. Beta.27 replaces it with a persistent sidebar shell modeled on the user settings page, plus dense list redesigns for the two highest-traffic admin pages.

### Persistent sidebar shell (#300)

- Replaces the card grid with a left rail that stays visible across all admin routes. Nav items are grouped: **Usage & Spend**, **AI Configuration**, **Identity & Access**, **Customization**.
- `/admin` redirects to `/admin/costs` as the default landing.
- Strips the redundant "Back to Admin" link from 10 top-level admin sub-pages — the sidebar replaces them.
- Cost summary cards restructured so the title gets its own row and the icon is a small top-right corner accent — fixes label wrapping on "Cache Savings" / "Avg Cost/User" in the narrower content area.
- Drive-by fix: 24 loading spinners across admin, settings, fine-tuning, and auth pages were rendering as a uniform gray ring in dark mode (no visible motion); they now spin with the proper accent.
- Admin shell widened and sidebar label wrapping fixed (#305).

### Route consolidations

Two clusters of sibling routes collapse into tabbed pages:

- **Quotas** (`/admin/quotas`) — Tiers, Assignments, Overrides, Inspector, Events. Five sibling routes become tabs on a single page; deep-link URLs are preserved for back-compat.
- **Fine-Tuning** (`/admin/fine-tuning`) — Access + Costs.

### Compact list redesigns

- **Manage Models + Bedrock/Gemini/OpenAI browse pages (#332)** — information-dense card layouts replaced with one-line scannable rows that expand on demand to show detail. Slim inline filter toolbar above the list. Inline enable/disable toggle on the manage-models row so status changes no longer require opening the edit form. Border-radius standardized on `rounded-2xl` to match the chat input.
- **Tool catalog + form (#335)** — same redesign applied to the admin tools list and create/edit form. Compact expandable rows with an inline detail panel. Form flattened to use the shared list-page token set (`rounded-2xl`, `text-sm/6`, `text-2xl/8` header, `focus:ring-2`) instead of the older heavy section cards. No behavior changes — purely visual.

### Admin-managed user-menu links (#298, #303, #315)

A new admin domain so org admins can curate the links shown in the SPA user menu without code changes. Each link is either an external URL (opens in new tab) or an in-app modal that renders admin-authored Markdown — covers the common cases of policy pages, feedback forms, and embedded org-specific notices.

- New `user-menu-links` DynamoDB table (single-tenant flat config; per-org PK scoping can be added later without changing the SK shape).
- Admin CRUD at `/admin/user-menu-links` (gated by `require_admin`).
- Public enabled-only read at `/user-menu-links` (cookie-aware `get_current_user_from_session` so it works under the BFF cutover).
- Links and in-app modals are visually distinguished in both the modal preview and the runtime rendering (#303).
- Resource gated to admin-only so non-admin user-menu loads no longer fire a duplicate request (#315).

### Sidebar density (#301)

Drive-by improvement on the chat session list: rows tighten from ~40px to ~32px (`py-2 → py-1.5`, `text-sm/6 → text-sm/5`), nested flex wrappers around the title removed (the link is now `block truncate` directly on the text), group gaps reduced (`gap-y-4 → gap-y-3`, `pb-1 → pb-0.5`, row `gap-y-1 → gap-y-0.5`). A list of 10 sessions is ~25% shorter overall. Inactive items drop from `font-medium` to `font-normal`; the active row picks up `!font-medium` via `routerLinkActive` so the selected state still feels distinct. Skeleton loader and entry animation added.

---

## Recoverable `max_tokens` Truncation

Previously a `MaxTokensReachedException` surfaced as a generic, leaky error in the chat (`...unrecoverable state... https://strandsagents.com/...`) and the only "recovery" was a re-send button that fired the original prompt as a new user turn — the model re-answered from scratch, hit the same ceiling, and infinite-looped (#328).

Beta.27 turns the failure into a first-class inline affordance.

### Backend

- `MaxTokensReachedException` is classified specifically in the stream processor; emits a `max_tokens`-coded, **recoverable** `stream_error` event. The leaked SDK URL and the verbose chat bubble are gone.
- **Continue is a resume, not a new turn.** A `continue_truncated` invocation re-enters the agent loop with an empty-list prompt, so the model continues the truncated assistant message in restored history (assistant-prefill) instead of answering a fresh instruction. Bypasses quota / RAG / file-resolution like the existing interrupt-resume path.
- The error is no longer double-persisted as a second assistant message (would otherwise break role alternation for the follow-up turn).
- **Refresh-survival.** A `lastTurnContinuable` marker on session metadata is set on truncation and cleared at the start of any non-resume turn. The marker flows through `SessionMetadataResponse` so Continue reappears after a page reload.
- `stream_error` is now an always-allowed parser event so a terminal recovery signal can't be dropped by stream-state gating.

### Frontend

- Compact inline "Response length limit reached" notice with a Continue button on the truncated message — no verbose error bubble.
- Continuation-aware message-map sync: pins the existing partial + notice and **appends** the continuation rather than truncating to the last user message.
- Hydrates `lastTurnContinuable` from session metadata on session load.

Backend + frontend regression tests cover classification, the continuation path, the always-allowed `stream_error`, and the refresh-survival marker round-trip.

---

## Model-Aware Adaptive Thinking + `effort`

Opus 4.7 rejects `thinking.type="enabled"` with a 400 — it requires adaptive thinking with depth governed by Anthropic's top-level `output_config.effort` field. Sonnet 4.6, Opus 4.6, and Mythos accept the legacy shape but recommend adaptive. Beta.27 makes `_shape_thinking_value` model-aware (#329, #330, #331).

- **Adaptive marker list.** `_BEDROCK_ADAPTIVE_THINKING_MARKERS = ("claude-opus-4-7", "claude-opus-4-6", ...)`. On a marker hit, `_shape_thinking_value` emits `{type: "adaptive", display: "summarized"}` (the explicit `display` keeps the reasoning trace visible — Opus 4.7 defaults `display` to `"omitted"`). Non-marker models keep the legacy `{type: "enabled", budget_tokens: N}` shape.
- **`effort` as a canonical inference param.** Routed through `additional_request_fields.output_config.effort` (it's NOT on `additionalModelRequestFields` like `thinking` / `top_k`). Wired through the admin model form and the user-facing chat settings panel as a new select control, with server-side allowed-set gating in the param normalizer.
- **Generic `allowed` enum on `ModelParamSpec`** — the per-model effort-tier difference between Sonnet 4.6 and Opus 4.7 (which gets the additional `xhigh` / `max` tiers) is now data, not a model-family branch in code.
- **Hardened param coercion (#329, #330).** `Dict[str, Any]` from JSON let a float reach the Bedrock Converse SDK, which rejects a float `maxTokens` with a hard boto3 validation error. `max_tokens` and `top_k` are now coerced to `int` at the single provider-translation chokepoint (covers fresh + resumed turns, all providers). The thinking-vs-`max_tokens` consistency guard previously used `isinstance(..., int)` and silently no-opped on float input; it now coerces first so an inconsistent request (`thinking >= max_tokens`) is rejected before reaching Anthropic. A model-ceiling cap protects against admin-configured `max_tokens` that exceed the model's hard limit.

---

## Inference-API Reliability

### `/ping` reaper fix (#338)

AgentCore's idle reaper requires an integer `time_of_last_update` field alongside `status`; when absent, the platform reaps the microVM at `idleRuntimeSessionTimeout` even mid-stream regardless of reported status (`bedrock-agentcore-sdk-python#471`). We have no async-task busy tracking yet (deferred async-mode work), so we cannot report `HealthyBusy` — returning a fresh timestamp on every ping is the documented mitigation against silent mid-generation reaps. Status casing also corrected to match `PingStatus`. This was a Kaizen-2026-05-15 review item.

### Removed dead Bearer-only auth from app-api (#297)

A sweep of `app_api/` for `Depends(get_current_user)`, `Depends(security)`, `Depends(verify_token)`, and manual `Authorization` header reads turned up exactly two routes still on Bearer auth, both in `chat/routes.py`. The dead Bearer-only paths are removed; `POST /chat/agent-stream` is documented as intentionally Bearer for non-SPA callers (API-key tooling, scripts). All other app-api routes are cookie-based BFF auth post-beta.24.

### Frontend version baking (#336)

`scripts/stack-frontend/build.sh` invoked `ng build` directly, which bypassed the npm `prebuild` lifecycle hook that runs `gen-version.js`. The deployed bundle therefore shipped the committed `'dev'` placeholder in `src/version.ts`, so the user menu rendered "local" on `develop` and `main`. Build script now runs `gen-version.js` explicitly before the build.

### A2A streaming-capability guard (#338)

Forward-looking guard: A2A is currently client-only. When the first A2A server construct lands (Strands `agent.to_a2a()`, `A2AServer`, or a hand-built `AgentCard`), its advertised capabilities **must** include `streaming=True` — otherwise the A2A SDK client silently falls back to non-streaming, never receives a `completed` event, and hangs ~40 minutes (ref-repo `sample-strands-agent-with-agentcore` commit `50c9112`). Documented in `CLAUDE.md` as a Kaizen-2026-05-15 review item.

### Misc inference-API polish

- Markdown content-type support in the artifact tool (#318).
- Configurable extra CSP `frame-ancestors` for artifacts (#314).
- `jsdelivr` and `unpkg` added to the artifact-origin script-src CSP so Chart.js artifacts loaded via the canonical jsDelivr snippet stop rendering blank (#326).

---

## Pre-Migration Backup Tool

A new `scripts/backup-data/` tool produces a complete, restore-friendly snapshot for a given `CDK_PROJECT_PREFIX`, plus a `workflow_dispatch` GitHub Actions workflow that runs it via the existing OIDC composite action (#361).

**Coverage:**

- All ~20 application DynamoDB tables via `ExportTableToPointInTime` (portable DynamoDB-JSON).
- User-content S3 buckets via `aws s3 sync`.
- Full Cognito user pool config including identity providers and app clients **with their plaintext client secrets preserved** (so IdP re-registration with new infra can be fully automated).
- Users, groups, and group memberships.
- Best-effort AgentCore Memory events.

Each run lands in a freshly-created, versioned, SSE-encrypted, TLS-only backup bucket named `{prefix}-backup-{utc_timestamp}`. `manifest.json` is the single source of truth a future restore script will consume.

**Known limitation:** Cognito password hashes are not exportable by AWS — that constraint is documented prominently. Ephemeral session/state tables are excluded by default. Restore is intentionally a separate phase, to be written against the new infrastructure once it exists.

---

## Smaller Improvements

- **Autofocus chat input on session load and switch (#333)** — focus the textarea on first mount and whenever the session changes (new or existing) so the user can type immediately. Assistant-preview empty state opts out via a new `autoFocus` input so it doesn't steal focus from the editor form.
- **Copy-to-clipboard button on chat code blocks (#299)** — plus Prism syntax-highlighting bundles for JavaScript, TypeScript, Python, and SQL alongside the existing C#/CSS bundles.
- **Tool renderer registry (#339)** — signal-backed `ToolRendererRegistryService` keyed by tool name replaces the implicit text/JSON/image switch baked into `ToolUseComponent`. Foundation for the MCP Apps `<mcp-app-frame>` renderer; `calculator`, `fetch_url_content`, and `create_visualization` migrated as proof points. Default renderer reproduces prior markup verbatim — zero visible change for existing tools.
- **Kaizen-2026-05-15 hygiene (#338, #341, #302, #304)** — replaced dead source URLs in `kaizen-research` (the `bedrock/whats-new/` 404, the `docs.claude.com` claude-code release-notes 301→404, and the inactive `anthropics/courses`); fixed `aws/amazon-bedrock-agentcore-{sdk-python,starter-toolkit}` repo-slug typos to the correct `aws/bedrock-agentcore-*` slugs.

---

## 🐛 Bug fixes

- `MaxTokensReachedException` no longer infinite-loops on retry; surfaces as a recoverable inline notice with Continue (#328).
- Float-typed `max_tokens` / `top_k` in inference params no longer crash boto3's Bedrock Converse client (#329, #330).
- Opus 4.7 no longer 400s on `thinking.type="enabled"` — model-aware adaptive shaping (#331).
- Silent mid-stream microVM reaping on long generations fixed via `time_of_last_update` (#338).
- Frontend deploy bundles bake the real version instead of the `'dev'` placeholder (#336).
- Chart.js artifacts loaded via `cdn.jsdelivr.net` no longer render blank (#326).
- Admin user-menu-links resource was firing a duplicate load request for non-admin users — gated to admin-only (#315).
- Artifact card z-index escapes its message row on focus — scoped with `isolation: isolate` (#323).
- `mcp-sandbox` CFN `Comment` overflow on the 128-char AWS cap (#356, #357).
- `mcp-sandbox` CSP not URL-decoded in CloudFront Function (#358).

---

## 🔒 Security / isolation

- **Artifacts** render on `artifacts.{domain}` — a different cookie-jar host from the SPA, with `connect-src 'none'` so an artifact cannot make outbound requests. Render-token JWTs are scoped to one `(artifact_id, version)` and are HMAC-signed with a Secrets-Manager-managed key. S3 versions are immutable: there's no `s3:DeleteObject` grant on the inference-api role.
- **MCP Apps** render on `mcp-sandbox.{domain}` with a per-resource `frame-ancestors` CSP emitted by a CloudFront Function. The outer host enforces a separate origin from the SPA, the inner App iframe carries `allow-same-origin` to match the basic-host reference, and an explicit user consent step (with reload persistence) gates first-time framing.
- App-api Bearer-only auth removed from all routes except the documented API-key endpoint (#297).

---

## ⚠️ Breaking changes

- **MCP Apps default-on.** `Defaults.MCP_APPS_HOST_ENABLED` flips `False → True`. To stay opt-in, set `AGENTCORE_MCP_APPS_HOST_ENABLED=false` in inference-api task env. If MCP Apps is enabled but `mcp-sandbox` isn't deployed, `ui_resource` events will emit with an empty `sandboxOrigin` and the SPA cannot frame the App.
- **App-api Bearer-only auth removed (#297).** If any external integration was calling `apis/app_api/` routes with `Authorization: Bearer`, switch it to the API-key feature (`auth/api_keys/`, `X-API-Key`) before deploying beta.27. `POST /chat/agent-stream` remains Bearer for non-SPA callers and is unaffected.
- **Opus 4.7 admin model entries.** Any admin model entry for an Opus 4.6/4.7 / Sonnet 4.6 / Mythos model that used `thinking.type="enabled"` should be updated to use the new `effort` knob; the runtime still emits the correct adaptive shape regardless, but the admin UI now exposes `effort` directly.

---

## 🏗️ Infrastructure

**New stacks (both gated by config flags, both safe to enable independently):**

- **`ArtifactsStack`** (gated by `config.artifacts.enabled`) — DDB `user-artifacts` table, private S3 `artifacts-content` bucket, render Lambda, CloudFront on `artifacts.{domain}`, Route53 alias. Consumes `/artifacts/render-token-key-arn` SSM (published by `InfrastructureStack`); publishes `/artifacts/bucket-name`, `/artifacts/bucket-arn`, `/artifacts/table-name`, `/artifacts/table-arn`, `/artifacts/origin`. Requires `CDK_HOSTED_ZONE_DOMAIN` and `CDK_ARTIFACTS_CERTIFICATE_ARN`.
- **`McpSandboxStack`** (gated by `config.mcpSandbox.enabled`) — S3 mount-page bucket, CloudFront distribution on `mcp-sandbox.{domain}` with a CloudFront Function for dynamic per-resource CSP, Route53 alias. Publishes `/mcp-sandbox/origin` SSM, consumed by inference-api at runtime as `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`.

**`InfrastructureStack` additions:**

- New `UserMenuLinksTable` (DDB) + `/admin/user-menu-links-table-name` and `/admin/user-menu-links-table-arn` SSM parameters.
- New `ArtifactRenderTokenSecret` (Secrets Manager, AWS-managed encryption, `generateSecretString` 64-char) gated on `config.artifacts.enabled`. SSM `/artifacts/render-token-key-arn` publishes the ARN. Lives in `InfrastructureStack` (not `ArtifactsStack`) so app-api can read it without taking a stack-deploy-order dependency on `ArtifactsStack`.

**Cross-stack:** `inference-api-stack` conditionally consumes `mcp-sandbox` SSM when `config.mcpSandbox.enabled` is true (mirrors the artifacts conditional-SSM pattern). Two synth tests cover present/absent.

**Deploy order:** `InfrastructureStack` → `ArtifactsStack` (if enabled) and `McpSandboxStack` (if enabled) → app-api → inference-api → frontend.

---

## 🔧 CI/CD improvements

- **Artifact env vars threaded through every consumer workflow (#307).** Validation on `config.artifacts.enabled` runs on every stack synth (the `bin/` instantiates all enabled stacks), so all five consumer workflows now pass `CDK_HOSTED_ZONE_DOMAIN`, `CDK_ARTIFACTS_ENABLED`, and `CDK_ARTIFACTS_CERTIFICATE_ARN` even when they're not synth'ing `ArtifactsStack` directly.
- **Backup workflow** — new `workflow_dispatch` job wired to the OIDC composite action, runs `scripts/backup-data/` against any `CDK_PROJECT_PREFIX` (#361).
- **Docker `curl` pin bumped (#327)** — Debian rotated `curl 8.14.1-2+deb13u2` out of the trixie apt index (superseded by `+deb13u3`), so the exact pin made every App API / Inference API Docker build hard-fail. Pin bumped, and the apt-pin policy documented as "follow Debian point-releases" rather than fully unpinning.
- **`infrastructure-stack` DDB count test (#350)** — replaced the brittle `resourceCountIs(18)` magic number (which went stale when `user-menu-links` landed) with an enumerated, justified table list. Infra Jest is the only gate here and nothing blocks merges on it, so the count assertion had been sitting red on `develop`.

---

## 📦 Dependency upgrades

- **`bedrock-agentcore` 1.6.4 → 1.9.1** (#337). Coupled `boto3` 1.42.96 → 1.43.9 with `botocore` / `s3transfer` following — `bedrock-agentcore` 1.9.1 requires `boto3>=1.43.0`. CHANGELOG audited end-to-end: no breaking changes for our memory/identity usage (the double-base64 fix is unused here, the namespace redesign is backward-compatible, the `ConversationTurn` fix is internal telemetry). Validated with a read-only dev smoke test (memory `get_memory_strategies` / `retrieve_memories` + identity `list_workload_identities`) and the full backend suite (2913 passed).

  Test-infra side effect: `botocore` 1.43 newly reads `Credentials.account_id` during endpoint construction; on a `RefreshableCredentials` (SSO) object that forces a refresh → `GetRoleCredentials`, which `moto` does not implement. Combined with `backend/src/.env`'s `AWS_PROFILE` leaking via `load_dotenv(override=True)`, this red-ed the suite order-dependently. Added per-test autouse scrub fixtures for `AWS_PROFILE` and the `DYNAMODB_*` / `COGNITO_*` config families, mirroring the existing `_clear_skip_auth_env` fixture for the same `.env`-bleed bug class.

- **`strands-agents` 1.39.0 → 1.40.0** (#340). Gated on a token-count audit and a compaction double-fire check. `use_native_token_count` default flipped `True → False` (Strands PR #2284) is inert for our token accounting — the flag gates only `BedrockModel.count_tokens()`, which Strands calls solely from `_estimate_input_tokens()` to populate `projected_input_tokens` on `BeforeModelCallEvent`. Our cost-badge / context-% / compaction-trigger plumbing reads from `inputTokens` + `cacheReadInputTokens` + `cacheWriteInputTokens` directly, so the default flip is transparent.

---

## 🧪 Test Coverage

- Backend + frontend regression tests for `MaxTokensReachedException` classification, the `continue_truncated` resume path, `stream_error` always-allowed gating, and the `lastTurnContinuable` refresh-survival marker round-trip (#328).
- Backend regression tests for adaptive thinking shape per model marker, `effort` allowed-set gating, and the float→int coercion path on `max_tokens` / `top_k` (#329, #330, #331).
- `infrastructure/test/mcp-sandbox-stack.test.ts` (264 lines) and `mcp-sandbox-csp-function.test.ts` (357 lines) — synth + CFN unit coverage for the new stack including the placeholder-substitution invariants and `frame-ancestors` quote-escaping.
- `infrastructure/test/inference-api-stack.test.ts` — two synth cases gating `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN` wiring on `config.mcpSandbox.enabled` (#349).
- `infrastructure/test/cors.test.ts` (53 lines) — new CORS test surface.
- Refactored `infrastructure/test/infrastructure-stack.test.ts` to enumerate the 19 DDB tables with one-line justifications instead of asserting a count (#350).
- Frontend specs for `mcp-app-bridge`, `mcp-app-card-state.service`, `mcp-app-consent.service`, `mcp-app-message.service`, `mcp-app-proxy.service`, `mcp-app-state.service`, `proxy-url`, `artifact-http.service`, `artifact-state.service`, `artifact-source.component`.

---

## 🚀 Deployment notes

This is a multi-stack release. **Read this section before deploying.**

### New stacks

If you want either feature, set the gating flag and the supporting env vars before synth:

- **Artifacts:** set `CDK_ARTIFACTS_ENABLED=true`. `CDK_HOSTED_ZONE_DOMAIN` and `CDK_ARTIFACTS_CERTIFICATE_ARN` become required across **every** consumer workflow that synthesizes any stack (validation runs on every synth — see #307). The artifacts ACM cert must be in `us-east-1` (CloudFront).
- **MCP Apps:** set the corresponding `mcpSandbox.enabled` config and `AGENTCORE_MCP_APPS_HOST_ENABLED` (now defaults true). The `mcp-sandbox` ACM cert must be in `us-east-1`. Without `mcp-sandbox` deployed, `ui_resource` SSE events will emit with an empty `sandboxOrigin` and the SPA cannot frame the App.

### Deploy order

1. `InfrastructureStack` (provisions `UserMenuLinksTable` + `ArtifactRenderTokenSecret` + SSM publishes).
2. `ArtifactsStack` (consumes `/artifacts/render-token-key-arn`).
3. `McpSandboxStack` (independent of `ArtifactsStack`).
4. `app-api` (consumes artifact + user-menu-links SSM).
5. `inference-api` (consumes artifact + mcp-sandbox SSM, conditional on flags).
6. Frontend.

### Auth migration

If any external integration was calling `apis/app_api/` routes with `Authorization: Bearer`, switch it to the API-key feature (`auth/api_keys/`, `X-API-Key`) before deploying beta.27 (#297). `POST /chat/agent-stream` remains Bearer-acceptable for non-SPA callers.

### Pre-migration safety net

Before any large infrastructure change (a stack-prefix migration, a region cutover, a CDK boundary refactor), run `scripts/backup-data/` first. The new workflow makes this a one-click affair against any `CDK_PROJECT_PREFIX`.

### Optional follow-ups (not deploy-blocking)

- Register an MCP Apps-capable MCP server via `step-04-deploy.md` to validate the host-renderer end-to-end against the committed `budget-allocator-server` example. Manual e2e dogfood scenario in `step-05-verify.md` exercises all six Definition-of-Done interactions.
- If you carry custom CSP `frame-ancestors` source lists for embedded preview environments, set `mcpSandbox.extraFrameAncestors` rather than rebuilding the CloudFront Function asset.

---

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
