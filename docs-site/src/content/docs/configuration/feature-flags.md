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
| Token exchange (RFC 8693) | `CDK_TOKEN_EXCHANGE_URL` (+ `_CLIENT_ID`) | **absent** | none | Optional external token-service exchange. Unset ⇒ no resources created |

## Runtime-only flags (on by default, no `CDK_*` variable)

These have **no CDK variable**. They are **all ON** and can only be turned off by
adding the env var directly to the container task definition. They are grouped
here because a fork operator will never find them by looking at `CDK_*`
variables. None of them costs extra against the model **except** tool summaries.

| Env var | Default | Cost | What it gates |
|---|:---:|:---:|---|
| `TOOL_SUMMARIES_ENABLED` | **ON** | **small — Nova Micro** | Model-generated one-line summary per tool batch. Off ⇒ falls back to free client-side formatters |
| `AGENT_STATUS_ENABLED` | ON | none | Live "what the agent is doing" status line + per-tool durations |
| `AGENT_STATUS_LIVE_DRAIN_ENABLED` | ON | none | Drains status during tool-execution silence (fixes bundled-late narration) |
| `AGENT_PREPARING_PHASE_ENABLED` | ON | none | Builds the agent inside the stream so a cold start is narrated, not dead air |
| `MID_TURN_STEERING_ENABLED` | ON | none | Inject a follow-up into a running turn at tool boundaries |
| `ANNOUNCEMENTS_ENABLED` | ON | none | Feature-announcement authoring + user-facing surfaces |
| `RESPONSE_FEEDBACK_ENABLED` | ON | none | Thumbs up/down on assistant messages |
| `ASK_USER_QUESTION_ENABLED` | ON | none | `ask_user_question` built-in tool (pauses a turn for structured input) |
| `BROWSER_TAKEOVER_ENABLED` | ON | none | `request_user_login` — hand the browser to the user to sign in |
| `BROWSER_TOOL_ENABLED` | ON | usage-based | The `browse_web` tool itself (AgentCore Browser sessions bill while active) |
| `WORKSPACE_TOOLS_ENABLED` | ON | none | `workspace_list` / `read` / `write` file tools |
| `DOCUMENT_READ_ENABLED` | ON | none | `document_read` tool for sessions carrying a readable attachment |
| `ATTACHMENT_TOOL_AUTOENABLE_ENABLED` | ON | none | Auto-injects spreadsheet-analysis tools when a spreadsheet is attached |
| `ATTACHMENT_TURN_GUARD_ENABLED` | ON | none | Enforces per-message file count + inline-byte budget |
| `ADMIN_ALWAYS_ON_TOOLS_ENABLED` | ON | none | Unions admin-flagged `alwaysOn` tools into every turn (inert with no data) |
| `COST_DIAGNOSTICS_ENABLED` | ON | none | Content-free behavioral counters for the admin session profile |
| `CONFIG_CACHE_ENABLED` | ON | **saves** money | In-process cache of tenant-global catalogs (fewer DynamoDB reads) |
| `AGENT_BUILD_SHARED_SESSION_ENABLED` | ON | none (**saves** ~0.5s on a cold first turn) | One process-wide boto3 session for the agent build's SDK clients (Memory session manager, strategy-id discovery, Bedrock model), built at container warm-up. Off ⇒ each SDK builds its own session, as before |
| `MCP_PARALLEL_PREFLIGHT_ENABLED` | ON | none (**saves** all but the slowest server's startup on a first turn with several external MCP servers — ~9.5s measured on dev with two cold servers) | Pre-flights an agent's external MCP servers concurrently when the agent is built, instead of one after another; clients are still registered in catalog order, so tool order (and the prompt-cache prefix) is unchanged. Off ⇒ servers load one at a time, as before |
| `MEMORY_RETRIEVAL_PREFETCH_ENABLED` | ON | none (**saves** ~200ms on every turn with long-term memory) | Starts the long-term-memory lookup as soon as the user's message is added, overlapping the two Memory writes the SDK awaits first; the result is applied at the same point as before, so persisted and live messages are unchanged. Off ⇒ the lookup runs after the writes, as before |
| `HISTORY_COUNT_PREFETCH_ENABLED` | ON | none (**saves** ~100–400ms on every warm turn, growing with conversation length) | Reads the per-turn count of stored messages (the base every per-message metadata index is computed from) on a worker thread started at the head of the turn, instead of before the model call; counts only messages created before the turn began, so the index is the one the inline read gave. Off ⇒ the count is read at the head of the turn, as before |
| `DOCUMENT_OFFLOAD_ENABLED` | ON | none | Document-context-offload pipeline (see spec) |
| `DOCUMENT_REHYDRATE_ENABLED` | ON | none | Re-injects offloaded document context on demand |
| `DOCUMENT_DIGEST_ENABLED` | ON | possible side-channel | Document digest step of the offload pipeline |

:::note
`SCHEDULED_RUNS_ENABLED`, `KB_SYNC_ENABLED`, `MEMORY_SPACES_ENABLED`,
`FINE_TUNING_ENABLED` and the Managed-KB vars also appear as raw env vars read by
background Lambdas/dispatchers — but you configure them through their `CDK_*`
variable in the table above, not by hand. The Lambda-side readers default to
*off* when unset, which is why CDK always sets them explicitly.
:::

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
- **`CDK_KB_SYNC_ENABLED`** — default **ON**. Runs a scheduled Lambda that
  re-embeds assistant KB sources; cost is the embedding calls + Lambda time on
  the schedule. Turn off if you do not use assistant knowledge bases.
- **`TOOL_SUMMARIES_ENABLED`** — default **ON**, and the one default-on flag with
  a recurring model cost: one bounded **Nova Micro** call per tool batch (a
  side-channel that never touches the cacheable prompt prefix). Cheap, but real.
  Set to `false` to fall back to free client-side tool formatters.
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

## Source of truth

- **Runtime flag readers + rationale**: `backend/src/apis/shared/feature_flags.py`
  (each flag documents its own default and why).
- **CDK-exposed flags + env wiring**: `infrastructure/lib/config.ts` and the
  `infrastructure/lib/constructs/*/*-environment.ts` maps.

If you add a flag, update this page in the same PR — that is the whole point of
having one canonical list.
