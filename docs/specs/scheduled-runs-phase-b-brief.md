# Phase B Scoping Brief — The Scheduled Trigger (F3)

**Status:** Handoff brief
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-07-05
**Parents:** `docs/specs/scheduled-agent-runs.md` (§4 architecture, §5 data model, §7 PR plan — the detailed design) · `docs/specs/agentic-platform-primitives.md` §6 (locked decisions) · `docs/specs/harness-entrypoint-spike-findings.md` (Phase A punch list)
**Builds on:** Phase A (merged — `apis/shared/harness/` + the `/runs/now` surface + `scheduled-runs` capability + `SCHEDULED_RUNS_ENABLED` flag on `develop`).

---

## 0. Where we are

Phase A shipped the keystone: `run_agent_headless(...)` runs a turn **as a user**, off the live socket, governed and delivered. Phase B is the **scheduled trigger** — a thin scheduler that calls that primitive on a cadence. It is *one trigger* on F1; keep it thin. The detailed design already exists in `scheduled-agent-runs.md` (§4/§5/§7) — this brief only records what's **new since that plan**: a gating prerequisite surfaced by the Phase A review, and the work breakdown + model tiering.

## 1. Gating prerequisite (B0) — the PII checkpoint ordering

**This blocks scheduled *delivery to real cohort users*, not schedule CRUD.** Read carefully; it's the one non-mechanical piece of Phase B.

The locked decision (primitives §6-4): **scheduled runs are fully unattended, so the F6a PII/data-classification checkpoint (`classify_output`) must be filled before the cohort can schedule.** But Phase A's review found that the checkpoint, as currently seamed, **can't do its job**: the runtime `/invocations` turn **persists the session + messages + title *during* the turn**, so by the time `classify_output` runs post-turn, unclassified output is already in the user's session list. A "before delivery" gate is too late.

**Design fork (needs Phil's call before B0 starts):**

- **Option A — In-loop classification.** Run the PII/FERPA checkpoint *inside* the invocation path, before the runtime persists the message. Cleanest guarantee (bad data never lands), but it modifies the agent loop / inference-api invocation path. ⚠️ Allowed only because it's a change to the *existing* `/invocations` path, **not a new inference-api route** (`CLAUDE.md` boundary) — verify that's how it's implemented, not a new endpoint.
- **Option B — Post-hoc scrub.** Let the turn persist, then have the harness **redact or delete** the persisted message/session when `classify_output` flags it. Stays entirely in `apis/shared`/app-api (no runtime change), but there's a brief window where unclassified data exists at rest, and "delete a just-written session" is fiddly.

**My recommendation: Option A**, because the whole point of the F6a floor for a FERPA context is that sensitive data *never lands*, and a post-hoc scrub with an at-rest window is a weaker story to defend. But A is more invasive — confirm before building.

Until B0 lands, the scheduler (B2) may be **built and merged with delivery disabled** (schedules fire in a dry-run / audit-only mode, or the worker is deployed but the EventBridge rule stays disabled), so B1/B2/B3 aren't blocked on the fork.

## 2. Work breakdown & model tiering

Per the agreed rule — **keystone/design-sensitive → top model (Fable 5); pattern-following fan-out → cheaper tier (Sonnet).**

| Item | What | Model | Depends on |
|---|---|---|---|
| **B1** | Schedule data model (`ScheduledPrompt`, sparse `DueScheduleIndex`) + app-api CRUD (`/schedules` — create/list/pause/delete), gated by the existing `scheduled-runs` capability + flag. **Inert**: schedules can be created but nothing fires yet. Mirror `apis/shared/sync_policies/`. | **Sonnet** | Phase A (done) |
| **B0** | Resolve the §1 fork; fill `check_input` (Bedrock `ApplyGuardrail`) + `classify_output` (PII/FERPA) for the scheduled trigger. Design-sensitive + touches governance/possibly the runtime. | **Fable 5** | Phil's fork decision |
| **B2** | Dispatcher + worker Lambdas + EventBridge rule (gated by flag) + IAM. Mirror `infrastructure/lib/constructs/kb-sync/` + `apis/app_api/kb_sync/`. Worker calls `run_agent_headless(trigger="schedule")`. **Delivery stays disabled until B0 lands.** | **Sonnet** | B1 |
| **B3** | SPA management UI (signal-based list/create/edit/pause under `frontend/ai.client/`). | **Sonnet** | B1 |

**Sequence:** B1 first (safe, unblocks B2/B3) → B0 ∥ B2 (B2 built, delivery off) → B3 → enable delivery once B0 merged → cohort scheduling. GA (grant capability to `default` + optional email) is a later phase.

## 3. Constraints & reuse (carry from the parent specs)

- **Model on `sync_policies`**: sparse "due" GSI, dispatcher/worker split, conditional re-arm (no double-dispatch), `paused_error` state. Model the Lambda plumbing on **KB-sync**.
- **Cohort gating already exists** — reuse the `scheduled-runs` capability + `SCHEDULED_RUNS_ENABLED` flag from Phase A; don't reinvent.
- **No inference-api routes** (B0 Option A modifies the existing invocation path only). Schedule CRUD is app-api, cookie auth.
- **`oauth_required` → pause the schedule** (the KB-sync `paused_reauth` analog) and surface the consent URL; a headless run can't pop a consent window. `run_agent_headless` already returns this as a first-class status.
- **Snapshot `enabled_tools` at schedule creation** (Phase A punch #7) — don't resolve "all RBAC-allowed" lazily at fire time, or the catalog shifting under a sleeping schedule causes least-surprise violations.
- **Runaway guards**: per-schedule max runs/day, auto-pause on repeated failure. Cadence → normalized `next_run_at` (timezone-aware) so the dispatcher stays a dumb "who's due" query — no cron strings in the engine.
- **HeadlessAuthError from the worker → pause the schedule**, don't error-spam (the grant expired / user hasn't logged in within N days).

## 4. Open decision for Phil

**The §1 fork: in-loop classification (Option A, recommended) vs post-hoc scrub (Option B).** This is the one architectural call that gates scheduled delivery. Everything else (B1/B2/B3) is pattern-following and can proceed now.
