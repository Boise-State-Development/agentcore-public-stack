---
name: smoke-regression
description: >-
  Run the smoke and regression pass for this stack after a change: the hermetic
  suites, the turn-matrix script that drives real agent turns and asserts every
  SSE code path (frame order, interrupt/resume, 409, Stop, preview, restore,
  cache stability), and the in-app-browser checklist for rendered surfaces.
  Use when the user says "smoke test", "regression test", "run the smoke pass",
  "verify this on dev", "test all the response paths", "did we break
  streaming", or before a release. Points at docs/testing/smoke-regression.md
  as the single source of truth for rows, fixtures and failure reading.
---

# Smoke / regression pass

The matrix, its fixtures and how to read a failure live in
[docs/testing/smoke-regression.md](../../../docs/testing/smoke-regression.md).
Read §2.2 before running L1 and §3 before L2. This skill is the procedure.

## 0. Decide the layers

From the diff, pick per §4 of the doc:

- **L0** always, for the packages touched.
- **L1** when the change touches `inference_api/chat/*`, `agents/main_agent/**`,
  `apis/shared/harness/**`, the SSE table, or any interrupt flow.
- **L2** when the change touches a rendered surface (composer, loader, tool
  rail, pickers, banners) or an interrupt flow.

Say which layers you are running and why in one line before starting.

## 1. L0 — hermetic

From the branch's checkout. In a worktree `PYTHONPATH` is mandatory or you test
`develop`:

```bash
cd <checkout>/backend && PYTHONPATH=$PWD/src AWS_EC2_METADATA_DISABLED=true <main checkout>/backend/.venv/bin/python -m pytest tests/ -q
```

Frontend `cd frontend/ai.client && npx ng test --watch=false`; infra
`cd infrastructure && npx tsc --noEmit && npx jest`. Never pass dummy AWS
credentials to make a stall go away (see the doc §1).

## 2. L1 — turn matrix

Pick the target in this order and state which one you used:

1. **Deployed dev through the Runtime** (always available to an agent with the
   `dev-ai` profile and an active headless grant):
   ```bash
   cd backend && AWS_PROFILE=dev-ai uv run python scripts/smoke_turns.py --auth headless-grant --user-id <sub> --prefix dev-boisestateai-v2 --json /tmp/smoke.json
   ```
   The grant's owner id is in `dev-boisestateai-v2-bff-sessions` under
   `PK=HEADLESS-GRANT#…` (`grant_user_id`). If the script reports no active
   grant, the user must sign in and run a headless run once; do not try to
   mint tokens another way (the doc explains why the other routes are dead ends).
   This mode skips the app-api-only rows (409, Stop, steer). It deletes the
   sessions it created; after a crash or `--keep`, pass `--delete-sessions id,id`.
2. **Local stack** when the branch is not deployed yet and you need the
   app-api rows: app-api on :8000 with `SKIP_AUTH=true`, inference-api on
   :8001, then `--base-url http://127.0.0.1:8000`. Do not flip `SKIP_AUTH` in
   the user's `backend/src/.env` without asking; use a worktree copy.
3. **Deployed through app-api** with a Cognito password user
   (`--auth cognito`), when one exists for the environment.

Add `--with-attachments` when attachments are in scope, and the fixture flags
(`--approval-tool`, `--skill-id`, `--agent-id`) when those rows matter. Never
pass `--allow-prod`.

Paste the report table into your summary. Treat SKIP as "not covered here",
OBSERVED as information, WARN as a model-quality note. A FAIL is a finding:
quote its detail, check the three traps in the doc §2.2 before calling it a
bug, then fix or report.

## 3. L2 — in-app browser

Use the built-in browser pane on `https://dev.boisestate.ai`; it carries the
user's session. Install the frame-capture snippet from the doc §3 with
`javascript_tool` first, then work through the checklist rows that apply.
Verify with `read_page` and `window.__frames`, not screenshots, except for a
final visual proof. Mirror the CSRF cookie into `X-CSRF-Token` for any manual
non-GET call. Delete the smoke conversations when done.

## 4. Report

One table per layer run, a one-line verdict, and the exact commands used so
the run is reproducible. If a layer was skipped, say which and why.
