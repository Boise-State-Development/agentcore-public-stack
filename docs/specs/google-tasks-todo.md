# Spec: Google Tasks Todo Integration

**Status:** Approved direction, not yet implemented
**Audience:** A fresh implementation session with no prior context — this doc is self-contained.
**Owner:** Phil Merrell
**Last updated:** 2026-06-23

---

## 1. One-line summary

Give students, staff, and faculty a conversational todo capability by **federating to Google
Tasks** — the assistant reads and writes tasks in the user's own Google account via the existing
AgentCore Identity OAuth flow. No new database. The headline value is **syncing Canvas deadlines
into Google Tasks**, so the list is never empty and tasks land on the user's phone automatically.

---

## 2. Decisions already made (do not re-litigate)

These were settled in a strategy discussion. Treat them as fixed inputs:

| Decision | Choice | Why |
|---|---|---|
| **Source of truth** | **Federate to Google Tasks** (not an owned DynamoDB store) | Counters the "empty walled-garden todo app nobody adopts" trap. Tasks appear instantly in Gmail sidebar, Google Calendar, and the Google Tasks mobile app. |
| **Institutional ecosystem** | **Google Workspace** | Boise State is a Google shop, so Google Tasks/Calendar is the native surface. |
| **Mechanism** | A **first-party agent tool** calling the Google Tasks REST API with a **user-delegated OAuth token from the AgentCore Identity vault** | The OAuth plumbing already exists; no DB, no custom service, no Gateway SigV4 hop needed. |
| **Surface (phase 1)** | **Plain local tool** returning structured results | Fastest path to validate the federation loop. |
| **Surface (later)** | **MCP App panel** (interactive inline checklist) | Highest-payoff UX for a todo list; additive, shares the same Google Tasks calls underneath. |

### Explicitly deferred / non-goals

- **No owned todo DynamoDB table.** Google is the store.
- **No Microsoft To Do / Graph.** Google-only.
- **No custom standalone todo service or external MCP server** in phase 1. (An MCP server only
  appears in phase 3, and only to get the App panel surface.)
- **No timed push reminders in phase 1** — see the Google Tasks constraints below.

---

## 3. Phasing

1. **Phase 1 — Federation loop (the foundation).** A local tool that lists / creates / completes /
   updates / deletes tasks in the user's Google Tasks `@default` list. Prove OAuth scopes, consent,
   and the API calls end-to-end. Returns structured text results.
2. **Phase 2 — Canvas → Google Tasks sync (the headline value).** Pull the user's Canvas assignment
   deadlines (the Canvas OAuth provider already exists) and write them into Google Tasks. This is the
   feature worth marketing; generic CRUD is table stakes.
3. **Phase 3 — Surface + timed reminders.** Wrap the capability in an **MCP App panel** for an
   interactive inline checklist, and use **Google Calendar** (same Google OAuth) for anything that
   needs a time-of-day reminder, since Google Tasks cannot (see §6).

Ship and validate Phase 1 before starting Phase 2.

---

## 4. Existing infrastructure to reuse

> File:line references are orientation pointers gathered during planning — **confirm them against
> the current code** before relying on them; line numbers drift.

### OAuth token retrieval (already built, production-ready)
- `apis/shared/oauth/agentcore_identity.py` — `AgentCoreIdentityClient.get_token_for_user()`.
  Signature (keyword-only):
  ```python
  async def get_token_for_user(
      *, provider_name: str, scopes: List[str],
      callback_url: Optional[str] = None,
      force_authentication: bool = False,
      user_id: Optional[str] = None,
      custom_state: Optional[str] = None,
      custom_parameters: Optional[Dict[str, str]] = None,
  ) -> TokenResult
  ```
- `TokenResult` (same file): `access_token: Optional[str]`, `authorization_url: Optional[str]`,
  and a `requires_consent` property (true when `access_token is None and authorization_url is not
  None`). Consent-required is a **normal outcome, not an error**.
- Google baseline `custom_parameters` (`access_type=offline`, plus `prompt=consent` on forced
  re-auth) is injected automatically by `custom_parameters_for(...)` / `_vendor_baseline_params(...)`
  in the same file. **Do not hand-roll these.**

### Consent surfacing (already built)
- `apis/shared/agents/main_agent/session/hooks/oauth_consent.py` — `OAuthConsentHook` is a
  **BeforeToolCall hook** that calls `get_token_for_user()` *before* the tool runs and, when consent
  is required, raises a Strands interrupt:
  ```python
  event.interrupt(name=f"oauth:{provider_id}", reason={
      "type": "oauth_required", "providerId": provider_id,
      "authorizationUrl": url,
  })
  ```
  which becomes the `oauth_required` SSE event the SPA already knows how to handle (popup → consent →
  `/api/connectors/complete-consent` → resume). **This is the path to reuse for consent — see §5
  Decision A.**

### OAuth provider records (where scopes live)
- `apis/shared/oauth/models.py` — `OAuthProvider` dataclass; `scopes: List[str]`,
  `provider_type: OAuthProviderType` (has `GOOGLE`), `custom_parameters`.
- `apis/shared/oauth/provider_repository.py` — read/write provider records in the
  `DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME` table.
- **Scopes are stored as DATA in DynamoDB, not in code.** The Google provider record's `scopes` list
  is the source of truth.

### Tool registration
- New first-party tool → `backend/src/agents/local_tools/<name>.py` decorated with
  `@tool` from `strands` (pattern: `local_tools/url_fetcher.py`).
- Export the tool functions in `backend/src/agents/local_tools/__init__.py`'s `__all__`.
- `backend/src/agents/main_agent/tools/tool_registry.py` discovers them via
  `registry.register_module_tools(local_tools)` in `create_default_registry()`.
- **Note a CLAUDE.md discrepancy:** CLAUDE.md says new tools go in
  `backend/src/agents/main_agent/tools/`. The live code uses `local_tools/` + `builtin_tools/`.
  Follow the live `local_tools/` pattern (confirm by reading `tool_registry.py`).

### Tool return shape
Strands-compatible dict, not a plain value:
```python
# success
{"content": [{"json": { ... }}], "status": "success"}
# error
{"content": [{"json": {"error": "..."}}], "status": "error"}
```

### RBAC / tool gating
- Tool access is granted per-role in DynamoDB AppRole records (`granted_tools` →
  `effective_permissions.tools`), see `apis/shared/rbac/models.py` and `service.py`.
- After building the tool, **the tool IDs must be added to the appropriate AppRole(s)** (e.g. a
  student role) or the tool stays invisible. Persona-scoped grants are a feature here, not a chore —
  students vs. faculty can get different task tooling via RBAC.

### MCP Apps host (for phase 3 only)
- Deployed and enabled by default (`AGENTCORE_MCP_APPS_HOST_ENABLED` default true; sandbox origin
  wired through `PlatformComputeRefs` → `AGENTCORE_MCP_APPS_SANDBOX_ORIGIN`). The `ui_resource` SSE
  path is emitted from `agents/main_agent/streaming/stream_coordinator.py`.
- **Important:** the `ui_resource` App-panel path is wired to **MCP** tools. A pure local Strands
  tool will not get an App panel. Phase 3 therefore means exposing the Google Tasks capability via an
  **MCP server that advertises a `ui://` resource**, not the phase-1 local tool. Plan for this fork.

---

## 5. Key design decisions to resolve FIRST

### Decision A — How consent is triggered (most important)

The established, working pattern is the **pre-flight `OAuthConsentHook`**: it acquires/refreshes the
token (and surfaces `oauth_required`) *before* the tool body runs, using a tool→provider mapping.
The tool body then calls `get_token_for_user()` expecting a token to be present.

- **Recommended:** wire the new Google Tasks tool(s) into the consent hook's tool→provider lookup so
  consent is handled pre-flight, exactly like existing OAuth-backed tools. Read `oauth_consent.py`
  and find how it resolves a tool_use to a provider (the `tool_use_provider_lookup` mechanism). This
  reuses the proven path and the SPA's existing `oauth_required` handling with zero new UX.
- **Avoid** the naive "tool returns an error dict with `authorization_url`" approach — the tool body
  has no access to the interrupt mechanism, so it cannot cleanly surface consent mid-execution. If
  the pre-flight hook cannot be made to cover a first-party local tool, escalate this as a blocker
  rather than inventing a parallel consent path.

**First task of the implementing session: read `oauth_consent.py` and confirm exactly how a tool is
associated with a provider, then extend that association for the new tool.**

### Decision B — Which Google provider record, and the scope

- Discover the **actual deployed Google provider id** (do not assume `"google-workspace"`) by
  inspecting the OAuth providers table / admin connectors UI. Use that id as `provider_name`.
- Add the Tasks scope **`https://www.googleapis.com/auth/tasks`** (read/write) to that provider
  record's `scopes` list. Use `tasks.readonly` only if you want a read-only variant.
- **Vault-key gotcha (known issue, see memory):** the token vault key includes `(provider, scopes,
  customParameters)`. Token retrieval must request the **same scopes and customParameters** used at
  consent time, or it falsely reports consent-required. Keep one canonical scope list + let
  `custom_parameters_for()` own the Google baseline params — don't pass ad-hoc customParameters.
- **Incremental-auth note:** adding the Tasks scope to an existing Google provider that already
  requests other scopes (e.g. Drive for RAG) means existing users will be re-prompted to consent to
  the widened scope. Decide whether to (a) widen the existing provider's scope list (one consent
  covers everything) or (b) request the tasks scope incrementally. Document the choice.

### Decision C — Where `user_id` comes from inside the tool

The consent hook gets `user_id` from the agent context. Confirm how the tool body obtains the current
user id in the inference-api agent loop (context injection vs. tool parameter) by following how
`oauth_consent.py` resolves `self._user_id`. Do **not** invent a new way to pass user identity.

---

## 6. Google Tasks API reference & hard constraints

Design within these — they shape what you can promise users:

- **Scope:** `https://www.googleapis.com/auth/tasks` (rw) or `.../tasks.readonly`.
- **Base:** `https://tasks.googleapis.com/tasks/v1`. Default task list = `@default`.
  - List tasks: `GET /lists/@default/tasks`
  - Create: `POST /lists/@default/tasks`
  - Update: `PATCH /lists/@default/tasks/{taskId}`
  - Complete: PATCH with `{"status": "completed"}`
  - Delete: `DELETE /lists/@default/tasks/{taskId}`
  - Task lists: `GET/POST /users/@me/lists`
- **Task fields:** `title`, `notes`, `due`, `status` (`needsAction` | `completed`), `parent`
  (one level of subtasks), `position`.
- **CONSTRAINT — `due` is date-only.** The API accepts an RFC-3339 timestamp but **discards the
  time of day and timezone**. "Submit by 3pm Friday" stores as just "Friday." Do not promise
  time-of-day due times via Tasks.
- **CONSTRAINT — no reminder/notification times via the API.** Google Tasks will not push a timed
  "remind me at 9am" notification; it only surfaces due-*dates*. Proactive timed nudging is **not**
  achievable with Tasks alone.
- **CONSTRAINT — thin metadata.** No priority, no tags, no custom fields, one subtask level.
- **Mitigation for the two constraints above (phase 3):** pair Tasks with **Google Calendar**
  (same Google OAuth) for time-sensitive items — Calendar events carry times and notifications.
  Frame it as "Tasks for to-dos, Calendar for timed reminders."

---

## 7. Phase 1 implementation checklist

1. **Confirm the consent wiring (Decision A)** — read `oauth_consent.py`; extend the tool→provider
   association for the new tool.
2. **Configure the provider (Decision B)** — find the real Google provider id; add the tasks scope
   to its DynamoDB record; document the incremental-auth choice.
3. **Build `backend/src/agents/local_tools/google_tasks.py`** with `@tool`-decorated functions, e.g.
   `list_tasks`, `create_task`, `complete_task`, `update_task`, `delete_task`. Each:
   - obtains `user_id` per Decision C,
   - calls `get_token_for_user(provider_name=<google id>, scopes=["https://www.googleapis.com/auth/tasks"], user_id=...)`,
   - calls the Google Tasks REST endpoint with the bearer token,
   - returns the Strands `{"content": [{"json": ...}], "status": ...}` shape,
   - translates the date-only `due` constraint clearly in its docstring (the docstring is the model's
     contract — state that time-of-day is not supported).
4. **Register** the functions in `local_tools/__init__.py` `__all__`.
5. **Grant** the new tool IDs to the appropriate AppRole(s) so they're visible to target users.
6. **Tests** — add pytest coverage (mock the Google API + the identity client). See §8.
7. **Manual verification** — exercise the consent popup → token → CRUD loop against a real Google
   account in a dev environment.

---

## 8. Conventions & guardrails (from CLAUDE.md / repo memory)

- **Backend pytest is the ONLY correctness gate — it is not run in CI.** Run the full local suite
  for any shared/auth/tool change:
  ```bash
  cd backend && uv sync --extra agentcore --extra dev
  uv run python -m pytest tests/ -v
  ```
- **Service boundaries:** `app_api`, `inference_api`, and `agents/` import only from `apis.shared`,
  never from each other (enforced by `tests/architecture/test_import_boundaries.py`). The tool lives
  under `agents/` and must use `apis.shared.oauth.*` for token retrieval.
- **Do NOT add routes to inference-api.** If any user-facing CRUD endpoint is needed (it should not
  be for phase 1), it goes in `app_api` with `Depends(get_current_user_from_session)`.
- **Exact version pins only** — no `^`, `~`, `>=`. **Never install a package without explicit user
  approval** (prefer `httpx`/stdlib already in the project over a new Google client library; confirm
  before adding any dependency).
- **Git:** branch from `develop` (never `main`); branch name `feature/google-tasks-todo`; PR targets
  `develop`; conventional commits (`feat:`, `fix:`, …); one logical change per commit; no
  commented-out code.
- **No `print()`** — use `logging`. Type hints on all signatures.

---

## 9. Open questions for the implementing session

1. **Consent wiring (Decision A):** does the pre-flight `OAuthConsentHook` tool→provider mechanism
   support a first-party local tool, or is it MCP/Gateway-specific? Resolve before writing the tool.
2. **Provider id & scope strategy (Decision B):** what is the real Google provider id, and do we
   widen its existing scope list or request the tasks scope incrementally?
3. **HTTP client:** confirm an approved HTTP library is already in `agents/` deps before coding the
   REST calls; do not add a Google SDK without approval.
4. **Multiple task lists:** phase 1 targets `@default` only — confirm that's acceptable, or whether
   per-persona task lists are wanted.

---

## 10. Why this shape (one paragraph for reviewers)

The expensive infrastructure — AgentCore Identity OAuth, the token vault, the `oauth_required`
consent UX, the Google provider, the Canvas provider, and the MCP Apps host — already exists. The
only genuinely new code is a thin tool that calls Google Tasks with a vaulted token. Federating to
Google (rather than owning a DynamoDB todo store) trades data ownership and a custom UI for instant
cross-surface presence (mobile, Gmail, Calendar) and zero new FERPA-relevant storage, and it makes
the real differentiator — Canvas deadlines flowing into the user's own task surface — a natural
phase 2 rather than a separate product.
