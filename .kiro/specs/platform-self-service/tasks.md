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

- [ ] 2.1 Establish a request-scoped `User`/`user_id` accessor for local tools,
  set when the agent is built for a turn (contextvar), mirroring how OAuth tools
  resolve identity. `backend/src/apis/inference_api/chat/app_tool_dispatch.py`
  (pattern source), agent factory / turn setup. _(Req 3.1)_

- [ ] 2.2 Helper `resolve_current_user()` for tools that fails closed (clear
  error) when context identity is absent. _(Req 3.3)_

- [ ] 2.3 Test: a tool cannot be made to act on a passed-in user id; identity
  always comes from context; absent context → fail closed. _(Req 3.2, 3.3)_

---

## Phase 3 — Skill 1 pilot: Account & Usage (read-only first)

- [ ] 3.1 `whoami` local tool (hidden, system): returns name, roles, tier from
  the session `User`. New file in `backend/src/agents/local_tools/`, exported in
  `__all__`, catalog entry `system=true, hidden=true`. _(Req 4.3)_

- [ ] 3.2 `get_my_quota` local tool (system, visible-but-locked): calls
  `QuotaChecker.check_quota(user)` and returns `current_usage, quota_limit,
  remaining, percentage_used, tier`. `backend/src/agents/main_agent/quota/checker.py`
  is the backing service. _(Req 4.1, 4.4)_

- [ ] 3.3 `get_my_settings` local tool (system, visible-but-locked): reads the
  user's settings (default model, …) via
  `backend/src/apis/shared/user_settings/repository.py`. _(Req 4.2, 4.4)_

- [ ] 3.4 Register all three in the catalog with correct `system`/`hidden`
  flags and friendly names/icons for transcript labeling.
  `backend/src/agents/main_agent/tools/tool_catalog.py`. _(Req 1.3, 4)_

- [ ] 3.5 Tests: each read tool returns correct data for the context user and
  refuses to target another; quota numbers match the enforcement path.

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
