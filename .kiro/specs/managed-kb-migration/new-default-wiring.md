# NEW_DEFAULT wiring — make new agents born managed

**Status:** Built (this branch) · **Part of:** `managed-kb-migration` (rollout ladder step 2)

## Problem
`MANAGED_KB_NEW_DEFAULT` is meant to make a newly created agent's knowledge base
**managed from birth**, skipping the Upgrade step. It did nothing:
- **Backend:** no code read `MANAGED_KB_NEW_DEFAULT`.
- **Infra:** the app-api Lambda received `MANAGED_KB_MIGRATION_ENABLED` but not
  `MANAGED_KB_NEW_DEFAULT`, so even a reader would find it absent.

## As-built design (reuse `enroll`, not a bespoke provisioner)

The original sketch proposed a dedicated eager-provision helper. Reading the code
changed the design: `provision_managed_kb` + the migration worker already do
crash-safe, dispatcher-driven provisioning, and `catch_up` already carries across
documents uploaded mid-flight. A brand-new agent has **no corpus**, so "born
managed" is just *migrating an empty corpus*: provision → converge instantly
(`migrated == total == 0`) → promote — through the proven machinery.

So born-managed = **call `enroll()` at agent finalize**, gated by the flag:

- `new_default_enabled()` reads `MANAGED_KB_NEW_DEFAULT` (allow-list of affirmative
  spellings, read at call time — mirrors `migration_enabled()`).
- `maybe_enroll_new_default(assistant_id, *, owner_user_id, visibility)`:
  no-ops unless the flag is on; calls `enroll()`; catches `UpgradeUnavailable`
  (migration worker off → nothing would finish the provision, so stay legacy) and
  swallows any other error (agent creation must never fail). Idempotent via
  `enroll`'s conditional writes.
- **Fire-and-forget** from the finalize path. `enroll` is two DynamoDB writes; the
  slow `CreateKnowledgeBase` happens later in the dispatcher-driven worker — so no
  minutes-long request-side task.
- **Call sites:** `create_assistant_endpoint` (direct COMPLETE) and
  `update_assistant_endpoint` **only on the DRAFT→COMPLETE transition**.

**Dependency (correct, not incidental):** born-managed needs the migration worker
running, so it has effect only alongside `MANAGED_KB_MIGRATION_ENABLED`. The
`UpgradeUnavailable` catch makes that graceful.

**Failure = legacy.** Any failure leaves `retrievalEngine` unset → the agent works
on the shared legacy index exactly as before.

## Infra
`app-api-environment.ts`: `MANAGED_KB_NEW_DEFAULT: String(config.managedKb.newDefault)`
next to the migration flag. `config.ts` already parses `newDefault`; `platform.yml`
already forwards the GitHub var. No other wiring.

## Risk gating TURN-ON (not this build)
Managed is one Bedrock KB per assistant; the account cap is ~10,000 KBs (there is
already an 80%-of-quota alarm). `NEW_DEFAULT=true` at fleet scale marches toward
that ceiling. Turning the flag on needs quota headroom, the KBs-by-engine
inventory (task 14.5), and likely a reaper for doc-less KBs. The wiring is safe to
ship dark.

## Tasks
- [x] 1. `new_default_enabled()` flag reader (`kb_upgrade/service.py`).
- [x] 2. `maybe_enroll_new_default()` — flag-gated, idempotent, error-swallowing,
      reuses `enroll()`.
- [x] 3. Fire-and-forget call site in `create_assistant_endpoint` (COMPLETE).
- [x] 4. Fire-and-forget call site in `update_assistant_endpoint` (DRAFT→COMPLETE).
- [x] 5. Infra: thread `MANAGED_KB_NEW_DEFAULT` into the app-api env.
- [x] 6. Backend tests: flag read (off/on/call-time); enrol-when-on;
      not-called-when-off (mutation guard); swallow `UpgradeUnavailable`; swallow
      unexpected errors.
- [x] 7. Infra test: `MANAGED_KB_NEW_DEFAULT` threaded + explicit `'false'` when off.
- [ ] 8. (Turn-on, later) verify Bedrock KB-count quota headroom before flipping on.
