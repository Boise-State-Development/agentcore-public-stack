# Spike Brief — Headless Agent-Run Entrypoint (F1) + Phase A Design

**For:** Fable 5
**Status:** Handoff brief — spike, not production
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-07-05
**Parents:** `docs/specs/agentic-platform-primitives.md` (F1, F2-minimal, F6a) · `docs/specs/scheduled-agent-runs.md` §4.2 (the risk) · `reference_dev_ai_aws_data` (dev-ai account)

---

## Why you're doing a spike first

The whole primitive layer (scheduled runs, proactive agents, A2A) rests on **one** capability that doesn't exist yet and is genuinely uncertain: **running an agent turn as a specific user, with no live browser session, and capturing the result.** Everything else in the plan is CRUD and pattern-following around this. So before any production code, prove the two hard unknowns and let them force the Phase A design. Over-invest *here*; economize everywhere after.

**Do not build:** the scheduler, the SPA, full Bedrock Guardrails, or the schedule data model. Those are Phase A/B proper. This spike de-risks F1 and produces the design others build against.

## Single success criterion

> Given `(user_id, prompt)` and **no live session**, a headless caller runs an agent turn **as that user**, the turn uses **at least one of that user's authorized connector tools** (e.g. lists a Google Drive file), a **governance checkpoint** sees the run, and the result lands as a **retrievable session** the user can open.

If you demonstrate that end-to-end **in dev-ai**, the spike succeeds. A localhost demo does **not** count — see the boundary note below.

## Critical boundary — must run in dev-ai, not localhost

The entire risk lives in the **AgentCore Runtime gateway auth**. Localhost (`:8001`) bypasses the runtime gateway entirely, so a local success proves nothing about the real question. Run against **dev-ai** (acct `490617140655`, `us-west-2`, prefix `dev-boisestateai-v2`; active runtime id via SSM `/dev-boisestateai-v2/inference-api/runtime-id`). Pick a test user who has a connector already authorized so the connector-token leg is real. (dev-ai SSO login gotcha: use Safari with `--no-browser`, not Brave — see `project_dev_ai_sso_login_gotcha`.)

## Unknown 1 — Unattended auth to the runtime (the core risk)

**Question:** Can a headless caller invoke the runtime `/invocations` *as a user* without a live Cognito token, such that (a) the agent loads that user's context and (b) it can retrieve that user's vaulted connector OAuth tokens?

- **Try first (decided approach):** invoke the runtime with the **platform workload identity**, passing `user_id` explicitly in the payload. This is exactly how the **KB-sync worker** already does unattended per-user work (mints via `GetWorkloadAccessTokenForUserId`, reads the user's vaulted OAuth without a session). Reuse that path.
- **Fallback if the runtime rejects a workload credential directly:** app-api mints a short-lived per-owner token that the caller relays. Only fall back if you *prove* the first path can't work — document why.
- **Reuse:** `apis/shared/oauth/agentcore_identity.py` (workload identity, `AGENTCORE_RUNTIME_WORKLOAD_NAME`), KB-sync IAM statements in `infrastructure/lib/constructs/kb-sync/kb-sync-construct.ts` (~152–184: `GetWorkloadAccessTokenForUserId`, `GetResourceOauth2Token`, secret read), runtime URL builder + request shape in `apis/app_api/chat/proxy_routes.py` (~33) and `InvocationRequest` in `apis/inference_api/chat/models.py` (~81).
- **Watch:** AgentCore `customParameters` are part of the token-vault key — token retrieval must use the **same** customParameters as the consent flow or it falsely reports consent-required (`project_agentcore_custom_parameters_vault_key`).

## Unknown 2 — Server-side SSE consumption

**Question:** Can the headless caller consume the `/invocations` SSE stream to completion (nothing today reads it server-side; the app-api proxy only relays to a browser)?

- **Build:** a small httpx SSE reader that drains to `message_stop`/`done`, extracts the **final assistant message**, and surfaces `stream_error`. Capture enough for a `RunResult` (see below): final text, tool-use trace, and cost/metadata if present.
- **Reuse:** event shapes in `agents/main_agent/streaming/event_formatter.py`; the SSE event table in `CLAUDE.md`. Mind the 300s runtime timeout (`proxy_routes.py`).

## Design deliverables (the real point of the spike)

Code proves feasibility; **these decisions are what Phase A builds on.** Produce a short design note answering:

1. **`run_agent_headless(...)` interface.** The signature and a `RunResult` shape (final message, tool trace, cost, status, error, `session_id`). This is the internal primitive every trigger will call — keep it **A2A-ready** (clean seam a future A2A server / runtime endpoint can front without a rewrite). ⚠️ If ever exposed as A2A, `capabilities` must include `streaming=True` (`CLAUDE.md`) or clients hang ~40 min.
2. **Where it lives.** It is *not* an inference-api route (only `/invocations` + `/ping` are reachable in cloud — `CLAUDE.md`). Recommend the module boundary: shared invocation helper vs. an app-api-hosted background caller vs. a dedicated Lambda. Justify against the service-boundary rule (`feedback_service_boundaries`).
3. **Auth approach chosen**, with evidence — which of Unknown-1's paths, and why.
4. **Governance-floor hook points (F6a seam only — do not implement guardrails yet).** Where does a run (a) write an **audit record**, (b) pass a **guardrails hook**, (c) hit a **data-classification checkpoint** on what it may read/emit? Place the seams so Phase A can fill them without retrofitting. For the spike, writing **one minimal audit record** is enough to prove the seam exists.
5. **Delivery (minimal F2).** Confirm result materialization via `ensure_session_metadata_exists` + `update_session_title` (`apis/shared/sessions/metadata.py` ~739 / ~560). Note the run-record shape.

## Out of scope for the spike

Scheduler / EventBridge / dispatcher-worker (Phase B) · SPA (Phase A-UI) · full Bedrock Guardrails + PII classifier implementation (Phase A, seams only here) · schedule data model · cohort RBAC wiring · email delivery.

## House rules (non-negotiable)

- No new routes on **inference-api**. · SPA-facing app-api routes use `Depends(get_current_user_from_session)` (cookie), never bare Bearer. · Exact version pins; **no new packages without Phil's approval**. · Branch from `develop`; conventional commits. · Prove in **dev-ai**, not localhost.

## Expected output

A spike branch demonstrating the success criterion in dev-ai, **plus** a design note (the 5 deliverables above) and a clear **go / no-go + chosen-auth-path recommendation** for Phase A. If it's no-go on the preferred auth path, the fallback analysis *is* the valuable result — say so plainly with the evidence.
