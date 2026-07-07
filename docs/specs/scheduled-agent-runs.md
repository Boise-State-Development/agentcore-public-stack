# Scheduled Agent Runs — Headless Run Entrypoint + Scheduled Trigger (Primitives F1 + F2 + F3)

**Status:** Draft / handoff spec (for implementation by Fable)
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-07-05
**Targets branch:** `develop` (PRs target `develop`, never `main`)
**Parent plan:** `docs/specs/agentic-platform-primitives.md` — this is the detailed design for its fundamentals **F1** (headless, trigger-agnostic agent-run entrypoint / the Harness slice), **F2** (async result + delivery spine), and **F3** (scheduled trigger). Read the parent for how these fit the wider primitive layer.
**Related:**
- KB-sync scheduled re-index — the scheduling template (`apis/shared/sync_policies/`, `infrastructure/lib/constructs/kb-sync/`)
- Unattended per-user OAuth (`apis/shared/oauth/agentcore_identity.py`, `AGENTCORE_RUNTIME_WORKLOAD_NAME`)
- Session metadata (`apis/shared/sessions/metadata.py`) — `ensure_session_metadata_exists`, `update_session_title`
- Assistants / Skills / RBAC (`apis/shared/assistants/`, `docs/specs/admin-skills-rbac-tool-binding.md`) — the run-config the entrypoint resolves + the cohort gate
- Per-user markdown memory (`docs/specs/user-markdown-memory.md`) — loaded *inside* a run, tracked separately (F5)

---

## 0. Instructions to Fable (read first)

Build a **generic primitive**, not a feature. The deliverable is a **headless, trigger-agnostic way to run an agent turn** ("run as user U, input P, resolved config C → structured result") plus a **scheduled trigger** on top of it and a **place for the result to land**. A chief-of-staff assistant ("Oliver"), briefings, digests, and monitoring are *validation use cases* (see the parent plan §5) — they must fall out as configuration, so keep every layer free of Oliver-specific assumptions.

**Ship to production, but scoped.** Each layer reaches prod while usable only by a small **feedback cohort** first. Widening the cohort — ultimately to *anyone who wants it* — must be a config change (grant a capability), not a re-architecture.

**Decisions already made (do not re-litigate; build to these):**

1. **The keystone is the headless run entrypoint (F1), not the scheduler.** The scheduler is *one trigger* on it. Build "run an agent turn off the live socket and capture the result" first and generically; a schedule, an A2A call, a webhook, or a "Run now" button are all just callers. Do **not** couple the entrypoint to scheduling.
2. **Model the scheduler on `sync_policies`**, not from scratch. It already solves the hard parts: a sparse "due" GSI, a dispatcher/worker split, conditional re-arm to prevent double-dispatch, and `paused_error` state. Copy that shape.
3. **Unattended runs invoke the agent as the run's owner** using the platform workload identity (the same mechanism KB-sync's worker already uses to read a user's vaulted OAuth tokens without a live session). The agent must run *as the user* so it can read their memory and use their connector tokens. **This is the single biggest technical risk — spike it first (PR-2/Phase A), before building UI on top.** ⚠️ If the entrypoint is ever exposed as an A2A server, `CLAUDE.md` requires its `capabilities` include `streaming=True` or clients hang ~40 min.
4. **Delivery starts as a run-record + an auto-created session** in the user's conversation list (e.g. titled *"Morning Briefing — Jul 6"*), using `ensure_session_metadata_exists` + `update_session_title`. **No new notification system in v1.** A lightweight inbox row and email-via-connector (the agent emails the user its own result as its final tool call) are high-value follow-ons — note them, don't block on them.
5. **Cohort gating uses RBAC**, not a net-new allowlist table. Create a `scheduled-runs` capability granted by a beta `AppRole`; add testers to it. GA = grant the capability to the default/everyone role. A global `SCHEDULED_RUNS_ENABLED` kill switch wraps the feature per house style (copy the `CDK_KB_SYNC_ENABLED` empty-string ternary exactly).
6. **The run-config is resolved from existing primitives** (an assistant id, a skill, an explicit tool set, a model) — the entrypoint doesn't invent a new config type. A validation use case like Oliver is then just "a system-seeded `Assistant` + a schedule pointing at it," with no bespoke code.

**House rules that bite here (from `CLAUDE.md` + prior art):**
- **Do not add routes to `inference-api`.** Only `POST /invocations` and `GET /ping` are reachable in cloud. All CRUD for schedules goes on **app-api**. The worker Lambda invokes the *runtime* `/invocations`, which is allowed.
- SPA-facing app-api routes use `Depends(get_current_user_from_session)` (cookie), never bare Bearer.
- Exact version pins; no new packages without explicit approval.
- Feature flags: **empty-string workflow-var gotcha** — `${{ vars.CDK_SCHEDULED_RUNS_ENABLED }}` is `""` when unset, which must resolve to the default, not "off". Copy the `CDK_KB_SYNC_ENABLED` ternary in `config.ts` exactly.

**When you hit a genuine fork not covered here, stop and ask Phil.** The open questions are in §8.

---

## 1. Summary

Let the agent **act without a live session**. Instead of only responding when a browser is open, a user (or admin) can register a schedule — *"every weekday at 7am, run this prompt as me"* — and that run happens unattended, as the user, with the result waiting in their conversation list when they log in. A validation use case: a system-seeded chief-of-staff assistant ("Oliver") a user schedules a morning briefing against — but the engine knows nothing about Oliver.

Three things are being built (parent-plan fundamentals in parentheses):

- **A headless run entrypoint (F1)** — "run agent as user U, input P, resolved config C → structured result," callable off the live socket by any trigger. The keystone.
- **A result + delivery spine (F2)** — a run-record plus result materialized as an auto-created session; a lightweight inbox/email as follow-ons.
- **A scheduled trigger (F3)** — user-owned schedules (`prompt + cadence + timezone + target config`), a dispatcher/worker pair that runs due schedules unattended. Modeled on `sync_policies`.

All ship to prod, gated to a `scheduled_runs_beta` RBAC cohort, behind a `SCHEDULED_RUNS_ENABLED` kill switch, with a documented one-config-change path to GA.

## 2. Goals

- A user can **create, edit, pause, and delete** a scheduled prompt from the SPA.
- Due schedules run **unattended, as the owning user**, with the owner's memory and connector tokens available.
- Results are delivered **idempotently** as a titled session the user can open — no duplicate spam if the dispatcher retries.
- The engine is **generic**: not hard-coded to any persona or to "briefings." Any resolved config (assistant / skill / tools / model) + any prompt + any cadence.
- **Cohort-scoped in prod**, widenable to GA by granting a role — no redeploy, no schema change.
- **Safe by default**: per-schedule runaway guards (max runs/day), auto-pause on repeated failure (`paused_error`), and a global kill switch.

## 3. Non-goals (v1)

- A new in-app notification / inbox / push system. Delivery is session-list discoverability (+ optional connector email).
- Per-assistant tool-binding UI (a scheduled run uses the config's/owner's RBAC-granted tools). Parent-plan F4-adjacent.
- Full per-user markdown memory. A run leans on system prompt + reference files + existing session continuity; memory is its own spec (`user-markdown-memory.md`, parent-plan F5) and lands in parallel.
- Arbitrary cron expressions in the UI. v1 offers a **bounded cadence set** (daily / weekday / weekly at an hour + timezone); store a normalized `next_run_at` so the engine stays cron-agnostic.
- Sharing/scheduling on behalf of *other* users. A schedule runs only as its own owner.
- Multi-step / conditional workflows. One schedule = one prompt = one run.

---

## 4. Architecture

```
                    ┌───────────────────────────────────────────────┐
   SPA  ── cookie ─▶│ app-api  /scheduled-runs/*  (CRUD)          │
                    │   → ScheduledPromptService (shared)            │
                    │   → DynamoDB (sparse DueScheduleIndex GSI)     │
                    └───────────────────────────────────────────────┘
                                        ▲ writes next_run_at
   EventBridge rule (rate 5 min,        │
   enabled iff SCHEDULED_RUNS_ENABLED) ─┐       │
                                ▼        │
                    ┌───────────────────────────────┐
                    │ Dispatcher Lambda             │  query DueScheduleIndex (next_run_at ≤ now)
                    │  - runaway + kill-switch check │  conditional re-arm (advance next_run_at)
                    │  - async-invoke worker per due │  → InvocationType="Event"
                    └───────────────────────────────┘
                                │ (one per due schedule)
                                ▼
                    ┌───────────────────────────────────────────────┐
                    │ Worker Lambda                                 │
                    │  1. mint owner workload token (WLI + user_id) │  ← agentcore_identity.py
                    │  2. POST runtime /invocations (as the user)   │  ← reuse InvocationRequest
                    │  3. consume SSE → final assistant message     │  ← net-new: server-side SSE reader
                    │  4. ensure_session_metadata_exists + title    │  ← deliver as a session
                    │  5. (follow-on) agent emails result via connector │
                    └───────────────────────────────────────────────┘
```

### 4.1 Reuse map (copy these, don't invent)

| Need | Reuse from | File |
|---|---|---|
| EventBridge rate rule, gated by `enabled` | KB-sync dispatcher rule | `infrastructure/lib/constructs/kb-sync/kb-sync-construct.ts` (~216) |
| Dispatcher → worker async invoke | KB-sync dispatcher | `apis/app_api/kb_sync/dispatcher.py` (~93) `InvocationType="Event"` |
| Sparse "due" GSI + conditional re-arm | `SyncPolicy` | `apis/shared/sync_policies/models.py`, `service.py` (`list_due_policies`, `rearm_policy`, `set_policy_state`) |
| Unattended per-user OAuth / workload token | KB-sync worker | `apis/shared/oauth/agentcore_identity.py`, `AGENTCORE_RUNTIME_WORKLOAD_NAME` + `GetWorkloadAccessTokenForUserId` |
| Runtime `/invocations` URL builder + request shape | chat proxy | `apis/app_api/chat/proxy_routes.py` (~33), `InvocationRequest` in `apis/inference_api/chat/models.py` |
| Idempotent session create + title | sessions | `apis/shared/sessions/metadata.py` (`ensure_session_metadata_exists`, `update_session_title`) |
| Docker Lambda + bootstrap-stub + SSM name export | KB-sync build | `kb-sync-construct.ts`, `backend/Dockerfile.kb-sync`, `bootstrap-assets/kb-sync/` |
| Global flag threading + empty-string ternary | KB-sync flag | `infrastructure/lib/config.ts` (`kbSync.enabled`), `apis/shared/feature_flags.py` |
| Cohort gating via role grant | Admin Skills RBAC | `apis/shared/rbac/`, `docs/specs/admin-skills-rbac-tool-binding.md` |

### 4.2 The hard part — unattended invocation (spike this first)

KB-sync proves a Lambda can act *as a user* for **connector tokens** (workload identity → `GetWorkloadAccessTokenForUserId` → vaulted OAuth). It does **not** prove a Lambda can drive a full **agent turn** unattended. Two gaps to close in the PR-2 spike:

1. **Auth to the runtime.** The runtime `/invocations` expects a bearer today (chat proxy relays the user's Cognito token). The worker has no live Cognito session. Validate invoking the runtime with the **platform workload credential**, passing `user_id` explicitly in the payload so the agent loads the right memory/session and mints the right connector tokens. If the runtime cannot accept a workload credential directly, fall back to app-api minting a short-lived owner token (see §8 Q1).
2. **Server-side SSE consumption.** No code today reads an `/invocations` SSE stream *inside* a Lambda — the app-api proxy only relays it to the browser. Build a small httpx SSE reader in the worker that drains the stream to `message_stop`/`done`, extracts the final assistant message (and surfaces `stream_error`). Net-new but contained.

**Deliverable of the spike:** a worker that, given a `user_id` + prompt, produces a stored session with a real agent answer that used at least one of the user's connector tools. Everything else in this spec is CRUD and wiring around that proof.

---

## 5. Data model

New records, keyed under the owning user. Follow `SyncPolicy`'s inert-record + sparse-index discipline: **the row is data; delete = total revocation; no orphan timers.**

```
PK = USER#{user_id}
SK = SCHEDPROMPT#{schedule_id}          # schedule_id = "sched-" + 12 hex

Attributes:
  scheduleId, userId
  assistantId: str | None               # target assistant's ast-id (None → default agent)
  label: str                            # "Morning Briefing"
  promptText: str                       # what to run
  cadence: "daily" | "weekday" | "weekly"
  hourLocal: int (0–23)
  weekday: int | None                   # for "weekly"
  timezone: str                         # IANA, e.g. "America/Boise"
  state: "active" | "paused" | "paused_error"
  nextRunAt: str                        # ISO 8601 UTC — recomputed on each run/edit
  lastRunAt, lastRunStatus, lastRunSessionId, lastError: optional
  runsToday, runsTodayDate: runaway guard
  deliverEmail: bool = false            # v1.5 connector-email opt-in
  createdAt, updatedAt

GSI: DueScheduleIndex (sparse)
  PK = "SCHEDDUE"                        # single hot-partition; fine at this scale (mirror GSI4)
  SK = nextRunAt                         # dispatcher queries nextRunAt ≤ now, oldest first
  only projected when state == "active"  # pausing/deleting removes it from the index
```

Cadence → `next_run_at` is computed in the service (timezone-aware) so the dispatcher stays a dumb "who's due" query. Never dispatch off a cron string.

## 6. Cohort gating & rollout

**Two independent controls:**

- **`SCHEDULED_RUNS_ENABLED`** — global kill switch. CDK context `CDK_SCHEDULED_RUNS_ENABLED` → `config.ts` (copy the `kbSync.enabled` ternary *exactly*, incl. the empty-string→default guard) → threaded into app-api **and** inference-api env, and gates the EventBridge rule's `enabled`. Default: **ON** in the env where we're testing, so the feature *exists* in prod; access is limited by the cohort, not by hiding the feature. (If we want it fully dark in an env, `=false`.)
- **`scheduled_runs_beta` AppRole** — *who* can create/run schedules. Grants: the `scheduled-runs` capability (plus visibility of any validation assistant seeded for the cohort). Add testers by mapping a JWT group (`jwt_role_mappings: ["group/scheduled-runs-beta"]`) or direct grant. This reuses the mature RBAC resolution path — no net-new allowlist table.

**Enforcement points:**
- app-api `/scheduled-runs/*` checks the caller resolves the `scheduled-runs` capability via RBAC; 403 otherwise.
- The dispatcher **re-checks** each due schedule's owner still has the capability before invoking (a revoked tester's schedules silently stop, don't error-spam).

**GA path (one change, no redeploy):** grant `scheduled-runs` to the default/everyone role (or wildcard). The feature flips from "beta cohort" to "anyone who wants it." Keep `SCHEDULED_RUNS_ENABLED` as the permanent kill switch.

## 7. PR plan

Sequence so the risky proof (F1) comes before the trigger and UI investment.

- **PR-1 — Headless run entrypoint (F1) + minimal result spine (F2).** The internal `run_agent_headless(user_id, input, config) → RunResult` primitive: workload-identity invocation of the runtime with explicit `user_id`, the server-side SSE reader, a run-record, and result materialized as an auto-created titled session. Expose a guarded **"Run now"** path (app-api, cookie auth, RBAC-gated) as the validation surface. **Land the §4.2 proof here — this de-risks everything.** No scheduling yet.
- **PR-2 — Schedule data model + CRUD API + cohort primitive (F3, control plane).** `ScheduledPrompt` model + service (shared), app-api `/scheduled-runs/*` (cookie auth, RBAC-gated), the `scheduled_runs_beta` role + `scheduled-runs` capability, `SCHEDULED_RUNS_ENABLED` flag threaded through CDK. Schedules can be created/listed; not yet fired.
- **PR-3 — Scheduler engine (F3, data plane).** Dispatcher + worker Lambdas (Docker, bootstrap stub, SSM name export), EventBridge rule gated by `SCHEDULED_RUNS_ENABLED`, IAM (mirror KB-sync). Dispatcher queries due → conditional re-arm → worker calls the PR-1 entrypoint. Delivery = the F2 session.
- **PR-4 — SPA management UI.** Signal-based schedule list/create/edit/pause under `frontend/ai.client/src/app/`. Pick a target config in the picker; "Schedule this" affordance. Runaway/error state surfaced to the user.
- **PR-5 — GA flip + docs + (optional) email delivery.** Grant `scheduled-runs` to the default role; release notes; and optionally the `deliverEmail` path (final tool call emails the result via the user's Gmail connector — a strong dogfood of the connector + tool-approval surface).

**Validation use case (separate, thin PR):** seed a system Assistant (e.g. "Oliver") + a schedule pointed at it, to prove the primitives end-to-end. No engine code — pure configuration.

**Parallel dependencies (not blockers):** per-user markdown memory (`docs/specs/user-markdown-memory.md`, F5) and first-class KB (F4) — either sharpens scheduled runs, but the engine must stand without them.

## 8. Open questions for Phil

1. ✅ **Unattended runtime auth — RESOLVED by the spike** (`docs/specs/harness-entrypoint-spike-findings.md`, 2026-07-05). The preferred workload-credential-as-bearer path is *provably impossible* (gateway JWT authorizer rejects the opaque workload blob; SigV4 refused). Chosen: **platform mints a per-owner Cognito access token** (`CognitoRefreshBearerAuth`), gated behind an explicit **headless-grant record** (own consent/revocation; "logged in within N days" policy). Workload identity keeps its real job — minting the user's connector tokens from the vault *inside* the run. See the findings doc's Unknown 1 for the two dead-ends with evidence.
2. **Delivery default.** v1 = session-in-list only, or turn on **connector-email** (`deliverEmail`) for the beta cohort from day one? Email is the better dogfood but adds the connector-token + tool-approval path to the critical path.
3. **Cohort mechanism.** RBAC `scheduled_runs_beta` role (recommended, reuses mature infra) vs. a `FEATURE_COHORT#scheduled-runs` sentinel item in the auth-providers table (lighter, but net-new gating code). Confirm RBAC.
4. **Run-config surface.** What can a schedule target in v1 — an Assistant id (recommended, shippable today), a Skill, or an explicit tool+model set? Assistant is simplest; Skill owns tool-binding but is behind the deferred `SKILLS_ENABLED`.
5. **Run frequency ceiling.** Max schedules per user and max runs/day per schedule for the beta (cost + spam guard)? Proposed: 5 schedules/user, 1 run/day/schedule for the cohort.

---

## Appendix — why this is the right dogfood

The capability we most lack for non-coding daily use is **proactivity**: today the agent only speaks when spoken to. A headless run entrypoint + scheduled trigger flips that — and because it's a *primitive*, a chief-of-staff persona, a weekly digest, and a monitoring alert are all just configuration on top (parent plan §5). Building it also forces us through — from the *user's* seat — the exact surfaces that are hardest and least dogfooded: unattended connector-token use, session creation outside a live chat, RBAC-scoped rollout, and the connector-email + tool-approval loop. Ship it narrow, live in it, widen it.
