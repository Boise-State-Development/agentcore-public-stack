# Spike Findings — Headless Agent-Run Entrypoint (F1)

**Status:** Spike complete — success criterion met in dev-ai
**Author:** Fable 5 (spike executed 2026-07-05, dev-ai acct 490617140655, us-west-2)
**Brief:** `docs/specs/harness-entrypoint-spike-brief.md` · **Parents:** `docs/specs/agentic-platform-primitives.md` (F1/F2/F6a), `docs/specs/scheduled-agent-runs.md` §4.2
**Spike branch:** `spike/harness-headless-entrypoint`

---

## Verdict

**GO for Phase A.** Chosen auth path: **platform-minted per-owner Cognito access token** (the brief's "fallback"), because the preferred path is *proven impossible* against the runtime gateway as deployed — see Unknown 1. Both hard unknowns are resolved with working code, and the success criterion ran end-to-end in dev-ai:

> Headless caller + `(user_id, prompt)`, no live session → agent turn ran **as the user** through the real AgentCore Runtime gateway → the turn called `search_classes` (the user's per-user-authorized class-search MCP connector, which validated the minted token downstream) → the **governance floor** wrote start/end audit records → the result landed as a **retrievable session** in the user's conversation list (title, metadata row, 2 persisted message items).

Evidence run: `run-8f10d164cff9` / session `headless-09a30f6ac87b4092` / user `18419330-70a1-7018-f8e3-9577b2e18455` (2026-07-05T21:32Z). Reproduce with:

```bash
cd backend && uv sync --extra agentcore --extra dev
AWS_PROFILE=dev-ai uv run python scripts/spike_headless_run.py \
  --user-id <cognito-sub> \
  --prompt "Use the class search tool to find two 3-credit undergraduate COMM classes..." \
  --tools class_search --title "Headless Spike — Class Search"
```

One scope note: the criterion's example connector ("lists a Google Drive file") could not run *inside* the turn because dev-ai has **no cloud-reachable vault-3LO agent tool** — the only 3LO tool (`canvas_faculty`) points at `http://localhost:8026`. That is a catalog gap, not an auth gap. The vault leg was proven separately and unattended: the same headless caller minted the user's **google-drive** token from the AgentCore vault via the platform workload identity (`GetWorkloadAccessTokenForUserId` → `get_token_for_user`, mirroring the consent flow's `customParameters` per the vault-key gotcha) and listed the user's actual Drive files. Connector auth is therefore proven on **both** protocols we have: forward-auth (in-run) and vault-3LO (out-of-run, identical mechanism to the in-run tool path).

---

## Unknown 1 — Unattended auth to the runtime (RESOLVED: fallback path)

The brief's decided approach — invoke `/invocations` with the **platform workload token** — **cannot work**, and not for a fixable configuration reason:

| Probe (dev-ai, real gateway) | Result |
|---|---|
| Workload access token as `Authorization: Bearer` | **403** `{"message":"OAuth authorization failed: Failed to parse token"}`. The token from `GetWorkloadAccessTokenForUserId` is an **opaque encrypted blob** (`AgV4…`, 1864 chars, not 3-segment JWT). The gateway's JWT authorizer can't even parse it, let alone validate issuer/client. |
| SigV4 `invoke_agent_runtime` (IAM data plane) | **AccessDeniedException:** *"Authorization method mismatch. The agent is configured for a different authorization method…"* — once a `customJWTAuthorizer` is configured (ours: Cognito discovery URL, `allowedClients=[BFF app client]`, `inference-agentcore-construct.ts` ~274), IAM invocation is refused outright. |

The brief's "try first" path conflated **two different trust boundaries**: the workload identity governs the *token vault* (connector OAuth, inside the run) — it was never a front-door credential. The front door only accepts a Cognito JWT for the allowed client. So the platform must mint a **real Cognito access token for the owning user**:

- **Spike implementation (works today, zero infra change):** exchange the user's stored BFF refresh token (`bff-sessions` table) via `REFRESH_TOKEN_AUTH` + SECRET_HASH — the exact machinery `SessionRefreshMiddleware`/`CognitoRefreshClient` already run. Verified: mints a 1-hour token with the right `sub`; the pool does **not** rotate refresh tokens (CDK default), so the user's live browser sessions are untouched (`rotated_refresh=False` observed).
- The minted token then works **three layers deep**: gateway JWT authorizer → container `get_current_user_trusted` (`sub` → user_id, so memory/RBAC/quota/session all resolve to the right user) → forwarded to forward-auth MCP servers, which validate `client_id == BFF client` (class-search accepted it).
- The **workload identity keeps its real job** unchanged: inside the run, connector tokens mint from the vault keyed by the `sub` of our bearer — no code change needed there.

**Phase A hardening (recommended, not blocking):** the refresh-token grant inherits BFF session lifetime (30-day absolute cap, sliding TTL) and is discovered by a table **Scan** (no user_id GSI). Ship Phase A on this path but behind an explicit **headless-grant record**: when a user enables scheduled runs, store a purpose-minted refresh token (or pin a session row) with its own lifecycle + revocation, and add a `user_id` GSI. "You must have logged in within 30 days for the platform to act as you" is a defensible governance default — make it a documented product decision. A dedicated M2M app client + trusted `user_id` payload was considered and rejected for Phase A: it requires container-side confused-deputy handling (`sub` = client-id, trust the payload) plus CDK changes to `allowedClients`, and it *weakens* the story that every downstream check sees a genuine user token.

## Unknown 2 — Server-side SSE consumption (RESOLVED: built and pinned to live shapes)

`apis/shared/harness/sse.py` — an httpx-based reader (`iter_sse_events`) + `InvocationStreamAccumulator` that drains to `done`, yielding a `RunResult` with final message, tool trace, usage, title, `stream_error`, and `oauth_required`. Non-obvious wire facts discovered live (unit tests pin them):

- The stream interleaves **typed events** with raw Strands passthrough (`event: event`); consume only the typed ones.
- `tool_use` arrives as `{"tool_use": {"tool_use_id", "name", "input"}}` where `input` is a **partial JSON string re-emitted repeatedly** as the model streams arguments — fold by id, keep the last parseable prefix. `tool_result` arrives **message-shaped** (`{"message": {"content": [{"toolResult": …}]}}`), not flat. (The flat `event_formatter` shapes also exist on other paths; both are handled.)
- Turn totals come on `metadata_summary` (cumulative), not the per-call `metadata` events — use the summary for cost attribution, per-call as fallback.
- A tool-use turn emits multiple `message_start/stop` cycles; "final message" = last completed non-empty assistant text.
- `session_title` may arrive mid-stream (or after `done`) and never carries the placeholder.
- 300s budget matches the proxy; `httpx.TimeoutException` → `status="timeout"`.

---

## Design deliverables

### 1. `run_agent_headless(...)` + `RunResult`

```python
async def run_agent_headless(
    *, user_id: str, prompt: str, auth: BearerAuthStrategy,
    session_id: str | None = None, run_id: str | None = None, title: str | None = None,
    # run-config mirrors InvocationRequest — no new config type (decision #6):
    model_id: str | None = None, rag_assistant_id: str | None = None,
    enabled_tools: list[str] | None = None, agent_type: str | None = None,
    inference_params: dict | None = None,
    trigger: str = "manual",                      # audit dimension: schedule|run_now|a2a|…
    invocations_base_url: str | None = None,      # env INFERENCE_API_URL fallback
    timeout_seconds: float = 300.0,
    governance: GovernanceFloor | None = None,
    on_event: OnEvent | None = None,              # A2A streaming seam
) -> RunResult
```

`RunResult` (`apis/shared/harness/models.py`): `run_id, session_id, user_id, status ∈ {completed, error, timeout, oauth_required}, final_message, stop_reason, error, title, tool_trace[{tool_use_id, name, input, result_preview, is_error}], usage{usage,metrics}, oauth_required[{provider_id, authorization_url}], started_at, finished_at, events_seen` — `to_dict()` is JSON-clean so it can become a run-record item or A2A task artifact untranslated.

**A2A-readiness:** the seam is `(user, input, config) → structured result` **plus** `on_event`, which relays every typed SSE event live — an A2A server front maps that onto task status updates without rewriting the runner. ⚠️ Standing CLAUDE.md rule: if exposed as A2A, `capabilities` must include `streaming=True`. `oauth_required` is a first-class status because a headless run cannot pop a consent window — schedulers should pause (KB-sync `paused_reauth` analog) and surface the URL.

### 2. Where it lives

**`backend/src/apis/shared/harness/`** — a shared invocation *client*, not a route. Justification against the service-boundary rules:

- It cannot be an inference-api route (only `/invocations` + `/ping` reachable in cloud) — and it doesn't need to be: the harness **calls** `/invocations`; the agent loop is unchanged.
- Its consumers span services: app-api ("Run now" route, PR-1 of scheduled-runs), the Phase-B dispatcher/worker Lambdas, a future A2A front. "Needed by more than one → `apis.shared`" (CLAUDE.md). `tests/architecture/test_import_boundaries.py` passes.
- The dedicated-Lambda option is a *deployment* choice for Phase B (the worker imports this module), not a module-boundary choice — same code either way.
- One dedupe noted in-code: `build_invocations_url` is a copy of the proxy's resolver; Phase A should point `proxy_routes.py` at the shared copy.

### 3. Auth approach chosen

**Platform-minted per-owner Cognito access token** (`CognitoRefreshBearerAuth`, `apis/shared/harness/auth.py`), behind a `BearerAuthStrategy` protocol so Phase A can swap in the headless-grant record without touching the runner. Evidence and the two dead ends are in Unknown 1 above; the negative probes are reproducible via `--probe-workload-token --probe-sigv4` on the driver script.

### 4. Governance floor (F6a) hook points

`apis/shared/harness/governance.py` — every headless run passes four checkpoints, placed so Phase A fills them without touching runner control flow:

1. **`on_run_start` — AUDIT (implemented).** Durable record *before* any token mint or model spend: `PK=USER#{user}, SK=RUN#{run_id}` in the sessions-metadata table — `trigger`, `promptSha256`, `promptChars`, `startedAt`, `status=started`. Invisible to all existing access paths (session listing queries `begins_with(SK,'S#ACTIVE#')`). Fail-closed: a run that can't be audited doesn't execute.
2. **`check_input` — guardrails seam (no-op).** Phase A: Bedrock `ApplyGuardrail(source=INPUT)`; blocked verdict raises before spend, leaving an audited failure.
3. **`classify_output` — data-classification seam (no-op).** Phase A: PII/FERPA checkpoint over `final_message` + tool-result previews, **before delivery**; may redact (mutate) or block (raise).
4. **`on_run_end` — AUDIT (implemented).** Outcome, stop reason, tool names, usage, finishedAt. Best-effort (a failed end-write can't destroy a delivered result; the start record pins existence).

Phase A promotions: run-records to their own table (or documented SK family) with a "recent runs per user" GSI; add the caller's IAM identity to the start record; wire `trigger` from real callers.

### 5. Delivery (minimal F2)

Confirmed — and cheaper than the spec assumed: **the runtime turn itself already materializes almost everything** (`/invocations` pre-creates the session row via `ensure_session_metadata_exists`, persists user+assistant messages, generates + persists a Nova title, updates activity/costs). Verified post-run: session row `S#ACTIVE#…#headless-09a30f6ac87b4092` (status active, messageCount 1→, title), **2 message items**, session visible in the user's list. The harness adds: idempotent `ensure_session_metadata_exists` (belt-and-braces for error paths) + `update_session_title` when the caller passes an explicit title (e.g. *"Morning Briefing — Jul 6"*), which wins over the generated one.

**Run-record shape** (the audit item; Phase A may split audit vs. delivery records): `runId, userId, sessionId, trigger, promptSha256, promptChars, status, stopReason, errorDetail, toolNames[], usage{...}, finalMessageChars, startedAt, finishedAt`.

---

## Phase A punch list (from spike scars)

1. Headless-grant record + `user_id` GSI (replace the BFF-table Scan); decide the "must have logged in within N days" policy explicitly.
2. Fill `check_input` / `classify_output`; promote run-records out of the sessions table.
3. "Run now" app-api route (cookie auth + RBAC capability) calling `run_agent_headless` — the PR-1 validation surface.
4. Dedupe `build_invocations_url` with the chat proxy.
5. Rotation-aware persistence in `CognitoRefreshBearerAuth` before anyone enables refresh-token rotation on the pool (currently warn-only).
6. Catalog gap, separate from Phase A: no cloud-reachable vault-3LO agent tool exists in dev-ai — deploy one (e.g. the canvas MCP server, or a Drive tool) so scheduled-run dogfooding exercises the 3LO path in-run.
7. `enabled_tools=None` means "all RBAC-allowed" — fine for "Run now", but schedules should snapshot an explicit tool set at creation (least surprise when the catalog changes under a sleeping schedule).

## Files (spike branch)

- `backend/src/apis/shared/harness/{__init__,models,sse,auth,governance,runner}.py` — the F1 primitive
- `backend/scripts/spike_headless_run.py` — dev-ai driver (positive proof + recorded negative probes)
- `backend/tests/apis/shared/test_harness_sse.py` — SSE shapes pinned from the live stream
- No new packages (httpx/boto3 already pinned); no spec files modified; no inference-api changes.
