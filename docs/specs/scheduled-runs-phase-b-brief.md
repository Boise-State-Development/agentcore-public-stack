# Phase B Scoping Brief — The Scheduled Trigger (F3)

**Status:** Handoff brief
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-07-05
**Parents:** `docs/specs/scheduled-agent-runs.md` (§4 architecture, §5 data model, §7 PR plan — the detailed design) · `docs/specs/agentic-platform-primitives.md` §6 (locked decisions) · `docs/specs/harness-entrypoint-spike-findings.md` (Phase A punch list)
**Builds on:** Phase A (merged — `apis/shared/harness/` + the `/runs/now` surface + `scheduled-runs` capability + `SCHEDULED_RUNS_ENABLED` flag on `develop`).

---

## 0. Where we are

Phase A shipped the keystone: `run_agent_headless(...)` runs a turn **as a user**, off the live socket, governed and delivered. Phase B is the **scheduled trigger** — a thin scheduler that calls that primitive on a cadence. It is *one trigger* on F1; keep it thin. The detailed design already exists in `scheduled-agent-runs.md` (§4/§5/§7) — this brief only records what's **new since that plan**: the governance model (role/auth-based, §1), and the work breakdown + model tiering (§2).

## 1. Governance model — role/auth-based, no content-scrubbing gate

**Decision (2026-07-05, revised — an earlier draft over-scoped this).** Governance for scheduled runs is **role/auth-based**, not content-classification-based. A scheduled run executes **as the owning user, with that user's own RBAC**, and delivers **back to that same user's own session list**. So it can only touch tools/KBs/connectors the user is already authorized for, and the result is visible only to the one person who could have pulled it interactively. **No new access boundary is crossed and no new recipient is introduced** — a PII/content-scrubbing pass would not prevent any exposure RBAC isn't already preventing.

The controls that actually apply, all already built:

- **RBAC (run-as-user)** — the run inherits exactly the owner's tool/model/KB grants. The primary control.
- **Grant lifecycle** — the headless-grant record (consent + revocation + N-days login-recency) is the real FERPA-relevant control on "the platform acts as you" (Phase A).
- **Quota + runaway guards** — cost ceilings + per-schedule max-runs/day + auto-pause on repeated failure.
- **Audit** — the fail-closed run-audit floor records who/what/when (Phase A).

**So B0 collapses — there is no PII-ordering fork and B2's delivery is not blocked.** Keep `check_input`/`classify_output` as **dormant no-op seams** (cheap optionality). They only become relevant if a run ever delivers to a *recipient other than the owner* (e.g. the deferred "email the result to someone else" feature) — governed *there*, not here.

## 2. Work breakdown & model tiering

Per the agreed rule — **keystone/design-sensitive → top model (Fable 5); pattern-following fan-out → cheaper tier (Sonnet).**

| Item | What | Model | Depends on |
|---|---|---|---|
| **B1** | Schedule data model (`ScheduledPrompt`, sparse `DueScheduleIndex`) + app-api CRUD (`/schedules` — create/list/pause/delete), gated by the existing `scheduled-runs` capability + flag. **Inert**: schedules can be created but nothing fires yet. Mirror `apis/shared/sync_policies/`. | **Sonnet** | Phase A (done) |
| **B2** | Dispatcher + worker Lambdas + EventBridge rule (gated by flag) + IAM. Mirror `infrastructure/lib/constructs/kb-sync/` + `apis/app_api/kb_sync/`. Worker calls `run_agent_headless(trigger="schedule")` — delivery flows straight through (governance is role/auth-based, §1). | **Sonnet** | B1 |
| **B3** | SPA management UI (signal-based list/create/edit/pause under `frontend/ai.client/`). | **Sonnet** | B1 |

**Sequence:** B1 first (safe, unblocks B2/B3) → B2 ∥ B3 → cohort scheduling. GA (grant capability to `default` + optional email) is a later phase. (B0 was dropped — see §1.)

## 3. Constraints & reuse (carry from the parent specs)

- **Model on `sync_policies`**: sparse "due" GSI, dispatcher/worker split, conditional re-arm (no double-dispatch), `paused_error` state. Model the Lambda plumbing on **KB-sync**.
- **Cohort gating already exists** — reuse the `scheduled-runs` capability + `SCHEDULED_RUNS_ENABLED` flag from Phase A; don't reinvent.
- **No inference-api routes.** Schedule CRUD is app-api, cookie auth.
- **`oauth_required` → pause the schedule** (the KB-sync `paused_reauth` analog) and surface the consent URL; a headless run can't pop a consent window. `run_agent_headless` already returns this as a first-class status.
- **Snapshot `enabled_tools` at schedule creation** (Phase A punch #7) — don't resolve "all RBAC-allowed" lazily at fire time, or the catalog shifting under a sleeping schedule causes least-surprise violations.
- **Runaway guards**: per-schedule max runs/day, auto-pause on repeated failure. Cadence → normalized `next_run_at` (timezone-aware) so the dispatcher stays a dumb "who's due" query — no cron strings in the engine.
- **HeadlessAuthError from the worker → pause the schedule**, don't error-spam (the grant expired / user hasn't logged in within N days).

## 4. Open decisions

None blocking. Governance is settled as role/auth-based (§1); B1/B2/B3 are pattern-following and proceed on the tiering in §2.
