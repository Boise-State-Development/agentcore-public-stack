# Platform Self-Service — Implementation Plan

Incremental, test-driven. Ship the pilot (Phase 3) end-to-end before adding
more skills or any destructive write. Each task references the requirements it
satisfies. Given the host resource note, run targeted tests per task, not the
full suite.

---

## Phase 1 — System / hidden tool tier (foundation)

- [ ] 1.1 Add `system: bool = False` and `hidden: bool = False` to
  `ToolDefinition` and `ToolMetadata`, with `from_dict` reading absent keys as
  false (mirror the existing `alwaysOn` migration).
  `backend/src/apis/shared/tools/models.py`,
  `backend/src/agents/main_agent/tools/tool_catalog.py`. _(Req 1.5, 1.6)_

- [ ] 1.2 Include `system` tool ids in the effective set for RBAC-granted users,
  independent of the admin `always_on` table and `ADMIN_ALWAYS_ON_TOOLS_ENABLED`.
  Extend `resolve_always_on_tool_ids` or add `resolve_system_tool_ids` +
  freshness snapshot.
  `backend/src/apis/shared/tools/always_on.py`,
  `backend/src/apis/shared/tools/freshness.py`,
  wired in `inference_api/chat/routes.py::_apply_admin_always_on_tools` and
  `voice_routes.py`. _(Req 1.1, 1.5)_

- [ ] 1.3 Filter `hidden` tools out of the settings toggle list in
  `get_user_accessible_tools`, while keeping them (flagged `hidden: true`) in the
  catalog payload used for display labeling.
  `backend/src/apis/app_api/tools/service.py`. _(Req 1.2, 1.3)_

- [ ] 1.4 Verify the streaming `tool_use` event still fires for a
  system/hidden tool (no code change expected; add a test that asserts it).
  `backend/src/agents/main_agent/streaming/stream_processor.py`. _(Req 1.4)_

- [ ] 1.5 Tests: defaults false; system tool survives a user "disable"
  preference; hidden excluded from toggle list but present in label payload.

---

## Phase 2 — Identity binding for account tools

> **Implemented via closure capture, not a contextvar (deviation from the
> design's tentative "contextvar preferred" note).** The repo's own code states
> the runtime does not populate Strands' ToolContext, and all six existing
> per-request tool families (spreadsheet/artifact/word/excel/powerpoint/
> workspace) bind identity by closure via `make_*_tool(...)` factories injected
> as `extra_tools`. Account tools follow that proven, safe pattern:
> `make_whoami_tool(user)` etc. in
> `backend/src/agents/local_tools/account_tools.py`. Req 3 (identity from
> context, never a model argument) is satisfied *more strongly* — the tool
> callables take **no** parameters at all, so there is no argument through which
> the model could target another user. "Fail closed when identity is absent" is
> structural: a tool cannot be built without a `User`.

- [x] 2.1 Identity captured by closure in the per-request factories (in place of
  a contextvar); wired at the `extra_tools` seam in
  `inference_api/chat/routes.py::_build_account_tools`. _(Req 3.1)_

- [x] 2.2 No `resolve_current_user()` needed — the factory requires a `User`, so
  a tool with absent identity cannot exist (fails closed by construction). _(Req 3.3)_

- [x] 2.3 Test: tools take no user/id argument (structural proof, `test_account_tools.py::TestIdentityBinding`); two tools bound to different users do not cross. _(Req 3.2, 3.3)_

---

## Phase 3 — Skill 1 pilot: Account & Usage (read-only first)

> Delivered behind the **`PLATFORM_SELF_SERVICE_ENABLED`** flag (default OFF)
> per the Rollout section. While off, no self-service `extra_tools` are added,
> so agent-cache eligibility and the cacheable prefix are exactly as before —
> zero per-turn cost. When on, the three tools are injected on every turn and
> close over only the invoking `User` (keyed by `user_id` in the cache key), so
> they are key-described and do not veto the agent cache.

- [x] 3.1 `whoami` (system, hidden): name/email/roles + quota tier from the
  session `User`. `agents/local_tools/account_tools.py::make_whoami_tool`. _(Req 4.3)_

- [x] 3.2 `get_my_quota` (system, visible-but-locked): read-only snapshot
  (`current_usage/quota_limit/remaining/percentage_used/tier`). **Computes via
  the resolver + cost aggregator, NOT `QuotaChecker.check_quota`** — the latter
  records warning/block events as a side effect, wrong for a read. _(Req 4.1, 4.4)_

- [x] 3.3 `get_my_settings` (system, visible-but-locked): reads default model
  via `apis.shared.user_settings.repository.get_user_settings_repository()`
  (new shared singleton, keeps agents off app_api). _(Req 4.2, 4.4)_

- [x] 3.4 Catalog metadata in `tool_catalog.py` with `system`/`hidden` flags and
  friendly names/icons for transcript labeling. _(Req 1.3, 4)_
  **Deferred:** seeding real DynamoDB `ToolDefinition` rows (unnecessary — the
  closures deliver the tools directly; `resolve_system_tool_ids` remains for any
  future *registry* system tool) and the SPA reading `hidden` to filter the
  settings panel (frontend task, task 1.3's frontend half).

- [x] 3.5 Tests: each read tool returns correct data for the context user;
  quota maps resolver+aggregator; unlimited/daily/no-tier/error paths; flag gate;
  catalog flags. `backend/tests/agents/local_tools/test_account_tools.py`
  (18 tests). _(Req 4)_

---

## Phase 4 — Platform-knowledge skill + base-prompt pointer

- [ ] 4.1 Author the platform-knowledge skill as a `system` + `internal` skill
  (summary line + body). Body = generated catalog + human-curated UI-placement
  section. _(Req 5.1, 5.4)_

- [ ] 4.2 CI generator: derive the machine part (setting keys, endpoints, tool
  names) from route definitions + settings models into the skill body; add a
  drift gate that fails CI on mismatch. Pin file I/O to `encoding='utf-8'`.
  _(Req 5.2)_

- [ ] 4.3 Route-guard test: fail if a UI route referenced in the human-curated
  section no longer exists. _(Req 5.3)_

- [ ] 4.4 Add the one-sentence base-prompt pointer to the account tools + the
  knowledge skill. `backend/src/agents/main_agent/core/system_prompt_builder.py`.
  Keep it tiny; no page-map, no per-user data. _(Req 5.1)_

---

## Phase 5 — Internal skill tier (skills plumbing)

- [ ] 5.1 Add `internal: bool` to system-owned skill records. _(Req 2.1, 2.2)_

- [ ] 5.2 Filter `internal` skills out of the user-facing "My Skills" list and
  block user edit/disable. `backend/src/apis/app_api/skills/` services. _(Req 2.2)_

- [ ] 5.3 Include `internal` skills in the invocable set for granted roles
  without a per-turn `enabled_skills` opt-in.
  `backend/src/apis/shared/skills/access.py` and the agent-binding resolver.
  _(Req 2.3, 2.4)_

- [ ] 5.4 Tests: internal skill invisible in management, always invocable for
  granted role, summary-only in context until invoked.

---

## Phase 6 — First confirmed write: `set_default_model`

- [ ] 6.1 `set_default_model(model)` local tool (system, visible-but-locked):
  validate the model is in the user's allowed set, then write only the invoking
  user's setting. _(Req 4.5)_

- [ ] 6.2 Confirmation flow: the agent states the exact change and requires
  explicit user confirmation before the write; KB/untrusted content cannot
  auto-trigger it. _(Req 6.1, 6.2)_

- [ ] 6.3 Post-write, the agent confirms what changed and that it applied only
  to the invoking user. _(Req 6.3)_

- [ ] 6.4 Tests: rejects a disallowed model; does not write without confirmation;
  writes scoped to context user only.

---

## Phase 7 — Follow-on skills (deferred, not part of the pilot)

Scaffolded by the same pattern once Phase 3–6 land and are validated in dev:

- [ ] 7.1 Skill 2 — Tools & Capabilities (`list_my_tools`, `is_tool_on`,
  `describe_tool`, `toggle_tool` [write · confirm]).
- [ ] 7.2 Skill 3 — Memory (`list_my_memory`, `search_my_memory`,
  `forget_memory` [write · confirm], backed by `/memory` + `/memory/search` +
  `DELETE /memory/{id}`).
- [ ] 7.3 Skill 4 — Conversations & Files.
- [ ] 7.4 Skill 5 — Connections.

**Never in any skill:** API-key mint/rotate, fine-tuning/inference jobs,
share/publish, `admin/*`.

---

## Rollout

1. Land Phases 1–3 (system tier + identity + read-only pilot) behind a feature
   flag; verify in dev that `whoami`/`get_my_quota`/`get_my_settings` work, that
   hidden tools stay out of the panel but show in the transcript, and that quota
   numbers match enforcement.
2. Land Phase 4 (knowledge skill + pointer) — validate "where is X" answers.
3. Land Phase 5 (internal skill tier).
4. Land Phase 6 (first confirmed write) only after the read path is proven.
5. Phase 7 skills follow individually.
