---
title: Feature Flags
description: Every feature flag in the stack — what it does, its default on a fresh fork, and whether it costs money.
sidebar:
  order: 2
---

This is the complete list of feature flags in the stack: what each one does,
whether it is **on or off on a fresh fork**, and whether it **incurs recurring
cost**. If you are standing up your own deployment and want to know "what am I
paying for by default, and what can I turn off?", this page is the answer.

:::tip[The one-minute version]
- **Most feature flags default ON** ("default on with a kill switch"). You opt
  *out* by setting the variable to `false`.
- **A few default OFF** and must be opted *into* — these are the ones with a
  real cost or a wide blast radius (Managed KB, feedback evaluations, MCP token
  enrichment).
- **Almost nothing costs extra just by being on.** The cost-bearing flags are
  called out explicitly in [Flags that cost money](#flags-that-cost-money).
- **Some runtime-only flags are temporary.** A flag marked *Rollout* exists
  only to undo a recent change quickly, and it will be removed. Don't build on
  its off position. See
  [Feature switches and rollout switches](#feature-switches-and-rollout-switches).
:::

## How flags work

A flag lives in one of two layers, and it matters which:

1. **CDK-exposed flags** — set with a `CDK_*` GitHub Actions variable (or a
   `cdk.context.json` key). CDK reads it at synth/deploy time and writes a
   plain env var (e.g. `SKILLS_ENABLED`) onto the container. **These are the
   flags you configure.**
2. **Runtime-only flags** — read directly from the container environment by the
   backend, with **no `CDK_*` variable wired up**. They are all ON by default
   and can only be flipped by hand-editing the task definition environment.
   See [Runtime-only flags](#runtime-only-flags-on-by-default-no-cdk_-variable).

The delivery chain for a CDK-exposed flag is:

```
GitHub Actions variable  →  platform.yml workflow  →  load-env.sh  →
cdk synth/deploy (config.ts)  →  container env var  →  backend reader
```

### Two idioms, and the empty-string rule

- **Default ON with a kill switch** (most flags): unset or empty resolves to
  **enabled**; only the literal string `false` disables it. This is deliberate
  — an *unset* GitHub Actions variable is forwarded as an **empty string**, and
  the empty string must mean "use the default (on)", never "off".
- **Default OFF, opt-in** (cost/blast-radius flags): only the literal string
  `true` enables it. An unset/empty variable stays off.

:::caution[Reading the code default can mislead you]
For several flags the Python reader's built-in default and the **deployed**
default differ, because **CDK sets the env var explicitly**. Example:
`memory_spaces_enabled()` in `backend/src/apis/shared/feature_flags.py` defaults
to *off* if the env var is unset — but every CDK deploy writes
`MEMORY_SPACES_ENABLED=true`, so a real deployment gets it **on**. **Trust the
"Default on a fork" column below, not the code default.** (The gap only exists
for local dev with no env var set.)
:::

## CDK-exposed flags

Set these with the named `CDK_*` GitHub Actions variable. "Default on a fork" is
what you get if you set nothing.

| Feature | `CDK_*` variable | Default on a fork | Recurring cost | What it gates |
|---|---|:---:|:---:|---|
| Skills (catalog, picker, authoring) | `CDK_SKILLS_ENABLED` | **ON** | none | Skills routes + runtime resolution |
| Scheduled / headless runs | `CDK_SCHEDULED_RUNS_ENABLED` | **ON** | when runs fire | "Run now" + headless-grant routes (dispatcher when Phase B lands) |
| Memory Spaces | `CDK_MEMORY_SPACES_ENABLED` | **ON** | none | `/memory/spaces` CRUD, runtime tools, SPA panel |
| Agent Designer | `CDK_AGENTS_API_ENABLED` | **ON** | none | The governed `/agents/*` authoring surface |
| Agent Marketplace | `CDK_AGENT_MARKETPLACE_ENABLED` | **ON** | none | Listing lifecycle, publisher profiles, admin Review/Listings (app-api only) |
| Fine-tuning | `CDK_FINE_TUNING_ENABLED` | **ON** | **only when a job runs** | Mounts `/fine-tuning` routes. SageMaker training bills per job; `defaultQuotaHours=0` keeps it whitelist-only |
| Artifact share inbox | `CDK_ARTIFACT_SHARE_INBOX_ENABLED` | **ON** | none | "Shared with you" tab (`GET /shared-artifacts`) |
| KB sync (scheduled re-index) | `CDK_KB_SYNC_ENABLED` | **ON** | **yes — scheduled** | EventBridge rule + kb-sync Lambdas that re-embed assistant KB sources |
| Managed KB — new default | `CDK_MANAGED_KB_NEW_DEFAULT` | **OFF** | **yes — storage** | New KBs provisioned on managed backend (~$5/GB-mo vs ~$0.15) |
| Managed KB — migration worker | `CDK_MANAGED_KB_MIGRATION_ENABLED` | **OFF** | **yes — storage** | Whether the background Migration_Worker runs at all |
| Managed KB — reconciler armed | `CDK_MANAGED_KB_RECONCILER_ARMED` | **OFF** | none (report-only until armed) | Reconciler *deletes* orphans vs only logging them |
| Managed KB — doc reconciler armed | `CDK_MANAGED_KB_DOC_RECONCILER_ARMED` | **OFF** | none (report-only until armed) | Dead-letter doc reconciler *corrects* rows vs only reporting |
| Feedback → Evaluations sampling | `CDK_FEEDBACK_EVAL_SAMPLING_ENABLED` | **OFF** | **yes — per eval run + PII** | Admin batch sends down-thumbed conversations to AgentCore Evaluations |
| MCP token enrichment | `CDK_MCP_TOKEN_ENRICHMENT_ENABLED` | **OFF** | **yes — Cognito plan** | Pre-token Lambda that copies pool attributes into access-token claims; forces Cognito **Essentials** feature plan (per-MAU cost) |
| MCP Apps host renderer | `AGENTCORE_MCP_APPS_HOST_ENABLED` | **ON** | none | Renders third-party MCP-server UI in the sandbox iframe (needs `mcp-sandbox` deployed) |
| Conversation index (archive writes) | `CDK_CONVERSATION_INDEX_ENABLED` | **OFF** (in development) | **yes — S3, KB storage** | Feature switch. Writes each finished turn's user and assistant text to the conversation-archive bucket that conversation search is built from, and enables the two EventBridge rules that index those turns into a shared managed knowledge base (created on the first indexed turn). Off stops new writes and indexing; deleting a conversation still removes its archived turns, but its index documents stay until the daily reconciler removes them (it runs whatever this flag says). See `docs/specs/conversation-search.md` |
| Direct user grants | `CDK_USER_GRANTS_ENABLED` (SPA: `features.userGrants`) | **OFF** (in development) | none | Feature switch. Mounts `/admin/user-grants` on app-api, where a system admin grants tools, models and skills to **one user** beside their roles (a pilot user, one researcher's model, a dated exception; grants are additive and carry an optional `expiresAt`). The flag is **app-api only**: permission resolution reads a user's grant row wherever it runs, including the AgentCore Runtime, which has no switch of its own (it is at its environment-variable cap, and an absent row is the off state). So `false` stops new grants and edits but does not revoke existing ones — delete the grant to revoke it. The surface is never scope-delegable (`admin.user_grants`): whoever can write a grant can name themselves. Set the backend variable and the SPA flag together |
| Conversation search | `CDK_CONVERSATION_SEARCH_ENABLED` (SPA: `features.conversationSearch`) | **ON** | only with the conversation index — KB retrievals | Feature switch, permanent. Serves `GET /sessions/search` on app-api and, with the SPA flag, the Cmd/Ctrl+K search dialog and the search button in the sidebar header. Searches the user's own conversations by title and opening prompt (DynamoDB) and, when the conversation index is on, on Enter or a pause by the full text of every turn ($0.001 per full-text search, at most 20 a minute per user). With the index off it matches titles and opening prompts only and makes no knowledge-base calls. Conversations created before this shipped are found by title only after `backend/scripts/backfill_session_search_attributes.py --apply` has run. `false` makes the route 404 and, with the SPA flag also `false`, leaves the sidebar with no conversation search. Set the backend variable and the SPA flag together. See `docs/specs/conversation-search.md` §5–§6 |
| Conversation retention — prune sessions | `CDK_CONVERSATION_RETENTION_PRUNES_SESSIONS` | **ON** | a few minutes of one Fargate task a day | Feature switch, permanent. Lets `CDK_CONVERSATION_RETENTION_DAYS` remove session rows too: a daily task deletes conversations whose last turn is older than the retention period, through the same cleanup as a user's delete, so they leave the sidebar instead of opening empty. Nothing is deleted until the next flag arms it. `false` keeps them as title-only rows and stops the task |
| Conversation retention — pruner armed | `CDK_CONVERSATION_RETENTION_PRUNE_ARMED` | **OFF** | none (report-only until armed) | Pruner *deletes* sessions past retention vs only logging how many it would (with the oldest and newest last-message time). Even armed, the first run in an environment, and the first run after the retention period changes, is a dry run |
| Token exchange (RFC 8693) | `CDK_TOKEN_EXCHANGE_URL` (+ `_CLIENT_ID`) | **absent** | none | Optional external token-service exchange. Unset ⇒ no resources created |

## Runtime-only flags (on by default, no `CDK_*` variable)

These have **no CDK variable**. They are **all ON** and can only be turned off by
adding the env var directly to the container task definition. They are grouped
here because a fork operator will never find them by looking at `CDK_*`
variables. Two of them cost extra against the model: tool summaries, and the
extra call compaction makes to pin facts. The **Kind** column says whether a flag
is permanent (*Feature*) or will be removed (*Rollout*, with the condition for
removing it).

| Env var | Default | Kind | Cost | What it gates |
|---|:---:|---|:---:|---|
| `TOOL_SUMMARIES_ENABLED` | **ON** | Feature | **small — Nova Micro** | Model-generated one-line summary per tool batch. Off ⇒ falls back to free client-side formatters |
| `AGENT_STATUS_ENABLED` | ON | Feature | none | Live "what the agent is doing" status line + per-tool durations |
| `AGENT_STATUS_LIVE_DRAIN_ENABLED` | ON | Rollout — eligible now (shipped ≤ 1.24.0) | none | Drains status during tool-execution silence (fixes bundled-late narration) |
| `AGENT_PREPARING_PHASE_ENABLED` | ON | Rollout — eligible now (shipped ≤ 1.24.0) | none | Builds the agent inside the stream so a cold start is narrated, not dead air |
| `MID_TURN_STEERING_ENABLED` | ON | Feature | none | Inject a follow-up into a running turn at tool boundaries |
| `ANNOUNCEMENTS_ENABLED` | ON | Feature | none | Feature-announcement authoring + user-facing surfaces |
| `RESPONSE_FEEDBACK_ENABLED` | ON | Feature | none | Thumbs up/down on assistant messages |
| `ASK_USER_QUESTION_ENABLED` | ON | Feature | none | `ask_user_question` built-in tool (pauses a turn for structured input) |
| `BROWSER_TAKEOVER_ENABLED` | ON | Feature | none | `request_user_login` — hand the browser to the user to sign in |
| `BROWSER_TOOL_ENABLED` | ON | Feature | usage-based | The `browse_web` tool itself (AgentCore Browser sessions bill while active) |
| `WORKSPACE_TOOLS_ENABLED` | ON | Feature | none | `workspace_list` / `read` / `write` file tools |
| `DOCUMENT_READ_ENABLED` | ON | Feature | none | `document_read` tool for sessions carrying a readable attachment |
| `ATTACHMENT_TOOL_AUTOENABLE_ENABLED` | ON | Feature | none | Auto-injects spreadsheet-analysis tools when a spreadsheet is attached |
| `ATTACHMENT_TURN_GUARD_ENABLED` | ON | Rollout — eligible now (shipped ≤ 1.24.0) | none | Enforces per-message file count + inline-byte budget |
| `ADMIN_ALWAYS_ON_TOOLS_ENABLED` | ON | Feature | none | Unions admin-flagged `alwaysOn` tools into every turn (inert with no data) |
| `COST_DIAGNOSTICS_ENABLED` | ON | Feature | none | Content-free behavioral counters for the admin session profile |
| `CONFIG_CACHE_ENABLED` | ON | Rollout — eligible now (shipped ≤ 1.24.0) | **saves** money | In-process cache of tenant-global catalogs (fewer DynamoDB reads) |
| `AGENT_BUILD_SHARED_SESSION_ENABLED` | ON | Rollout — retires after the Runtime V2 migration, once a V2 snapshot restore is clean | none (**saves** ~0.5s on a cold first turn) | One process-wide boto3 session for the agent build's SDK clients (Memory session manager, strategy-id discovery, Bedrock model), built at container warm-up. Off ⇒ each SDK builds its own session, as before |
| `MCP_PARALLEL_PREFLIGHT_ENABLED` | ON | Rollout — retires after one prod release + 2 weeks clean | none (**saves** all but the slowest server's startup on a first turn with several external MCP servers — ~9.5s measured on dev with two cold servers) | Pre-flights an agent's external MCP servers concurrently when the agent is built, instead of one after another; clients are still registered in catalog order, so tool order (and the prompt-cache prefix) is unchanged. Off ⇒ servers load one at a time, as before |
| `KB_SEARCH_AHEAD_ENABLED` | ON | Rollout — retires after one prod release + 2 weeks clean | none (**saves** up to the knowledge-base search's round trips — ~0.4s measured on dev — on an agent turn with a knowledge base) | Starts an agent turn's knowledge-base search where it always ran but awaits it after the agent build, ahead of the citation frames; the augmented message and citations are byte-identical. Off ⇒ the search is awaited before the build, as before |
| `MEMORY_RETRIEVAL_PREFETCH_ENABLED` | ON | Rollout — retires after one prod release + 2 weeks clean | none (**saves** ~200ms on every turn with long-term memory) | Starts the long-term-memory lookup as soon as the user's message is added, overlapping the two Memory writes the SDK awaits first; the result is applied at the same point as before, so persisted and live messages are unchanged. Off ⇒ the lookup runs after the writes, as before |
| `HISTORY_COUNT_PREFETCH_ENABLED` | ON | Rollout — retires after one prod release + 2 weeks clean | none (**saves** ~100–400ms on every warm turn, growing with conversation length) | Reads the per-turn count of stored messages (the base every per-message metadata index is computed from) on a worker thread started at the head of the turn, instead of before the model call; counts only messages created before the turn began, so the index is the one the inline read gave. Off ⇒ the count is read at the head of the turn, as before |
| `INLINE_ATTACHMENT_PERSIST_ENABLED` | ON | Rollout — retires after one prod release + 2 weeks clean | none | Writes a headless caller's inline spreadsheet or deck bytes to S3 as session files before the turn runs, so the tools the guidance note names can reach them. The SPA uploads first, so it never takes this path. Off ⇒ diverted inline attachments are dropped, as before |
| `COMPACTION_SUMMARY_EXTRACT_ENABLED` | ON | Rollout — retires after a production quality readout of extract-then-compress | one extra side-channel call per compaction cut | Pins standing instructions, decisions, identifiers and latest values verbatim ahead of the compaction summary (two concurrent calls). Off ⇒ the single plain compression call |
| `DOCUMENT_OFFLOAD_ENABLED` | ON | Rollout — retires once the quality check waived in `docs/specs/document-offload-evaluation.md` has run | none | Document-context-offload pipeline (see spec) |
| `DOCUMENT_REHYDRATE_ENABLED` | ON | Rollout — retires with `DOCUMENT_OFFLOAD_ENABLED` | none | Re-injects offloaded document context on demand |
| `DOCUMENT_DIGEST_ENABLED` | ON | Rollout — retires with `DOCUMENT_OFFLOAD_ENABLED` | possible side-channel | Document digest step of the offload pipeline |

:::note
`SCHEDULED_RUNS_ENABLED`, `KB_SYNC_ENABLED`, `MEMORY_SPACES_ENABLED`,
`FINE_TUNING_ENABLED` and the Managed-KB vars also appear as raw env vars read by
background Lambdas/dispatchers — but you configure them through their `CDK_*`
variable in the table above, not by hand. The Lambda-side readers default to
*off* when unset, which is why CDK always sets them explicitly.
:::

## Feature switches and rollout switches

Every default-on flag is one of two kinds:

- **Feature switch**: turns off a capability you may reasonably not want,
  because it costs money, widens what users can do, or is a product choice.
  These are configuration and are permanent. Every CDK-exposed flag is a
  feature switch or an opt-in.
- **Rollout switch**: guards a change that is meant to be invisible to users,
  such as a reordering on the chat path, a fix or a cache. It exists so the
  maintainers can undo a bad deploy without reverting code. Nobody needs the off
  position once the change is proven, and an off path that nobody runs stops
  working without anyone noticing. So rollout switches **are removed**.

A rollout switch normally retires after it has shipped in a production release
and run there for at least two weeks with no one needing to flip it. Some
switches wait for a stricter condition, which the Kind column states.
Switches waiting to be retired are tracked in
[issue #1422](https://github.com/Boise-State-Development/agentcore-public-stack/issues/1422).
Retirements are batched into one cleanup per release cycle, and each removed
variable is listed under **Removed** in the
[CHANGELOG](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/CHANGELOG.md).

**If you've set a rollout switch to `false`**, treat it as temporary and tell
the maintainers why, through an issue. A rollout switch someone actually needs
turned off is a bug report, and it stays until that bug is fixed.

## Flags that cost money

If you care about the bill, these are the only flags that move it:

- **`CDK_MANAGED_KB_NEW_DEFAULT` / `CDK_MANAGED_KB_MIGRATION_ENABLED`** — managed
  KB storage bills ~**$5/GB-month** (vs ~$0.15 for the legacy backend). Both
  default **OFF** for exactly this reason. Leave them off unless you have
  decided to pay for managed retrieval.
- **`CDK_FEEDBACK_EVAL_SAMPLING_ENABLED`** — each admin-triggered batch sends
  conversation spans (system prompt + user messages) to an AWS-managed
  evaluator: a per-run cost **and** a PII exposure decision. Default **OFF**.
- **`CDK_MCP_TOKEN_ENRICHMENT_ENABLED`** — turning it on forces the Cognito user
  pool onto the **Essentials** feature plan, which bills per monthly active
  user. Default **OFF**.
- **`CDK_CONVERSATION_INDEX_ENABLED`** — default **OFF** while in development.
  Writes one small S3 object per finished turn (cents a month at today's scale)
  and indexes it into a managed knowledge base billed at ~$5/GB-month, through
  an SQS queue and a container Lambda (cents). Estimated ~$4/month for ~13k sessions, ~$45 at 30k users
  (`docs/specs/conversation-search.md` §8).
- **`CDK_CONVERSATION_SEARCH_ENABLED`** — default **ON**. Title and
  opening-prompt searches are DynamoDB queries (cents). Full-text search runs
  only when `CDK_CONVERSATION_INDEX_ENABLED` is also on, at one knowledge-base
  retrieval ($0.001) per search. Capped at 20 full-text searches a
  minute per user; estimated ~$1/month today, ~$18 at 30k users
  (`docs/specs/conversation-search.md` §8).
- **`CDK_KB_SYNC_ENABLED`** — default **ON**. Runs a scheduled Lambda that
  re-embeds assistant KB sources; cost is the embedding calls + Lambda time on
  the schedule. Turn off if you do not use assistant knowledge bases.
- **`TOOL_SUMMARIES_ENABLED`** — default **ON**, with a recurring model cost:
  one bounded **Nova Micro** call per tool batch (a side-channel that never
  touches the cacheable prompt prefix). Cheap, but real. Set to `false` to fall
  back to free client-side tool formatters.
- **`COMPACTION_SUMMARY_EXTRACT_ENABLED`** — default **ON**. Each compaction cut
  makes one extra summary-model call to pin facts verbatim. Cuts are infrequent,
  so the extra cost is small. This is a rollout switch, so plan on it going
  away rather than turning it off to save money.
- **`CDK_FINE_TUNING_ENABLED`** — default **ON**, but the routes are free; only a
  SageMaker training job actually launched by a user bills, and
  `CDK_FINE_TUNING_DEFAULT_QUOTA_HOURS=0` keeps it admin-whitelist-only.
- **Observability** (`CDK_OBSERVABILITY_XRAY_SAMPLING_RATE`,
  `CDK_OBSERVABILITY_AGENTCORE_APPLICATION_LOGS_ENABLED`) — X-Ray bills per trace
  recorded (default sampling 1%); AgentCore application logs are **off by
  default** because they are high-volume and carry full prompts/responses.

## Access-control settings (not feature rollout)

These look like flags but govern *access*, not feature availability. They
deliberately **default to the closed/safe posture**, inverting the "default on"
rule.

| Setting | `CDK_*` variable | Default | Notes |
|---|---|:---:|---|
| Cognito self-signup | `CDK_COGNITO_SELF_SIGNUP_ENABLED` | **OFF (closed)** | Opens the Hosted UI "Sign up" link to the public internet. Federated sign-in and first-boot admin are unaffected |
| Gateway inbound auth | `CDK_GATEWAY_INBOUND_AUTH` | `iam` | `iam` or `jwt`. **Immutable after Gateway creation** — changing it needs a new Gateway |
| Browser URL blocklist | `CDK_BROWSER_URL_BLOCKLIST` | **empty** | Hosts Chromium refuses during a browser takeover. Empty ⇒ RBAC on `browse_web`/`request_user_login` is the only control (the deploy log warns) |
| Local-dev auth bypass | `SKIP_AUTH` | **OFF** | `SKIP_AUTH=true` returns a fake admin. **Local dev only** — a CI guard refuses any PR that puts it in deployable config, and app-api refuses to boot if it is paired with a non-localhost CORS origin |

## Non-boolean tuning knobs

Beyond on/off flags, these numeric/string knobs shape cost and behavior. They
follow the same env → context → default precedence. See
[`infrastructure/lib/config.ts`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/infrastructure/lib/config.ts)
for the authoritative list and defaults:

- **Managed KB byte caps**: `CDK_MANAGED_KB_PER_OWNER_BYTES` (100 MB),
  `CDK_MANAGED_KB_PER_OWNER_ELEVATED_BYTES` (1 GB),
  `CDK_MANAGED_KB_PER_KB_CEILING_BYTES` (500 MB), plus fleet alarms
  `CDK_MANAGED_KB_STORAGE_ALARM_GB` / `CDK_MANAGED_KB_DAILY_COST_ALARM_USD`.
- **Observability thresholds**: log retention, X-Ray sampling, ALB/Lambda/ECS
  alarm thresholds, per-model Bedrock TPM quotas — all `CDK_OBSERVABILITY_*`.
- **App-API sizing**: `CDK_APP_API_CPU` / `_MEMORY` / `_DESIRED_COUNT` /
  `_MAX_CAPACITY`.
- **Conversation retention**: `CDK_CONVERSATION_RETENTION_DAYS` (365). A
  conversation's content is kept for this many days after each turn, wherever it
  is stored; nothing about a user's long-term memory records (facts,
  preferences) changes. Whole days, at least 3. Today it sets AgentCore Memory's
  event expiry, which stops at 365 even if the value is higher, and the
  conversation archive's lifecycle rule; a daily reconciler deletes archive
  objects and search-index documents past it, and, once armed, sessions whose
  last turn is past it (`CDK_CONVERSATION_RETENTION_PRUNES_SESSIONS`,
  `CDK_CONVERSATION_RETENTION_PRUNE_ARMED` above). Unset or empty means 365.

## Source of truth

- **Runtime flag readers + rationale**: `backend/src/apis/shared/feature_flags.py`
  (each flag documents its own default and why).
- **CDK-exposed flags + env wiring**: `infrastructure/lib/config.ts` and the
  `infrastructure/lib/constructs/*/*-environment.ts` maps.

If you add a flag, update this page in the same PR — that is the whole point of
having one canonical list. Give a rollout switch its retirement condition in the
Kind column when you add it.
