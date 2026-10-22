# Platform Self-Service — Requirements

## Introduction

Users on boisestate.ai routinely ask the assistant questions that are really
about **this platform and their own account** on it — "how much of my quota is
left?", "why can't you read my spreadsheet?", "where do I change my default
model?", "what do you remember about me?". Today the agent cannot answer these:
it has no knowledge of the app wrapped around it and no way to read the user's
own platform state.

This feature gives the agent two capabilities, kept deliberately separate:

1. **Knowledge** — the agent can explain how the platform works and where
   settings live (progressive-disclosure skill, not always-on prompt bloat).
2. **Account access** — the agent can read (and, with confirmation, change) the
   invoking user's *own* platform state through the existing user-scoped APIs.

To support this cleanly, it also introduces a **system tier** for tools and
skills: platform-shipped capabilities that are always available, not
user-toggleable, and hidden from the settings management UI — while still
visible in the session transcript when the agent uses them.

### Scope guardrails (non-negotiable)

- Every tool acts **only on the invoking user's own data**, through an existing
  user-scoped app-api endpoint. **Never** raw DynamoDB, never another user.
- Identity comes from the **validated session**, never from a tool argument or
  the prompt (consistent with `PLATFORM_SAFETY_FLOOR` in
  `backend/src/agents/main_agent/core/system_prompt_builder.py`).
- **Reads are free; writes require an in-conversation confirmation.**
- Out of scope, human-only, never exposed to a skill: minting/rotating API
  keys, launching fine-tuning or inference jobs, sharing/publishing to other
  users, and anything under `admin/*`.

---

## Requirement 1 — System tier for tools

**User story:** As a platform operator, I want certain platform-shipped tools
(e.g. `whoami`) to always be available and NOT appear in the user's Tools panel,
so that internal plumbing tools cannot be toggled off and do not clutter the
settings UI.

#### Acceptance Criteria

1. WHEN a tool is marked `system` THEN it SHALL be treated as always-on and the
   user's tool preferences SHALL NOT be able to disable it.
2. WHEN a tool is marked `hidden` THEN `GET /tools/` SHALL exclude it from the
   user-facing toggle list returned to the settings Tools panel.
3. WHEN a `hidden` tool is returned in any catalog payload used for display
   labeling THEN it SHALL still carry its display metadata (name, icon) flagged
   `hidden: true`, so the frontend can label a tool-use event without offering a
   toggle.
4. WHEN the agent invokes a `system`/`hidden` tool during a turn THEN the
   streaming `tool_use` event SHALL be emitted normally, so the user sees the
   agent using it in the session transcript.
5. WHEN a `system` tool is defined THEN its provenance SHALL be distinguishable
   from an admin-configured `always_on` tool (system = shipped with the app, not
   a per-deployment admin knob).
6. IF the `system`/`hidden` classification is absent on a tool record THEN it
   SHALL default to a normal, user-toggleable, visible tool (backward
   compatible with every existing tool row).

---

## Requirement 2 — System tier for skills

**User story:** As a platform operator, I want internal skills to be distinct
from user-created skills, so that platform capabilities are always available,
cannot be edited or disabled by users, and do not appear in the user's skill
management UI.

#### Acceptance Criteria

1. WHEN a skill is `system`-owned THEN it SHALL reuse the existing
   `owner_id == "system"` catalog provenance
   (`backend/src/apis/app_api/skills/service.py`).
2. WHEN a skill is marked `internal` THEN it SHALL NOT appear in the user's
   "My Skills" management list and SHALL NOT be user-editable or user-disabled.
3. WHEN skill access is resolved for a turn THEN internal skills the user's role
   grants SHALL be available without an explicit per-turn opt-in (they are
   platform capabilities, not opt-in extras).
4. WHEN an internal skill loads THEN only its summary line SHALL be in context
   until it is invoked (progressive disclosure, consistent with existing
   `skill://` loading behavior).

---

## Requirement 3 — Per-turn identity binding for account tools

**User story:** As a security reviewer, I want account tools to resolve the user
from request context rather than a model-supplied argument, so that the agent
can never read or change another user's data.

#### Acceptance Criteria

1. WHEN an account tool executes THEN it SHALL resolve `user_id` / `User` from
   the same request-context path OAuth tools use
   (`backend/src/apis/inference_api/chat/app_tool_dispatch.py`), NOT from a tool
   parameter.
2. IF a tool schema would require the caller to pass a user identifier THEN the
   design SHALL be rejected — identity is never a model-visible argument.
3. WHEN identity cannot be resolved from context THEN the tool SHALL fail closed
   with a clear error rather than acting on a default or guessed user.

---

## Requirement 4 — Skill 1 pilot: Account & Usage

**User story:** As a user, I want to ask the assistant about my usage, quota, and
settings and get a direct answer, so that I do not have to hunt through the UI.

#### Acceptance Criteria

1. WHEN a user asks how much quota they have left THEN `get_my_quota` SHALL
   return current usage, limit, remaining, percentage, and tier, computed via
   the existing `QuotaChecker`
   (`backend/src/agents/main_agent/quota/checker.py`).
2. WHEN a user asks about their settings THEN `get_my_settings` SHALL return the
   user's settings via the existing user-settings surface (e.g. default model).
3. WHEN a user asks who they are / their role THEN `whoami` SHALL return name,
   roles, and tier from the validated session/user record.
4. WHEN any Skill 1 read tool is invoked THEN it SHALL require no confirmation.
5. WHEN `set_default_model(model)` is invoked THEN it SHALL (a) validate the
   model is one the user is allowed to use, (b) require an in-conversation
   confirmation, and (c) change only the invoking user's setting.
6. WHEN a user asks "where" a capability lives (e.g. the usage page) rather than
   "how much" THEN the agent SHALL answer from the platform-knowledge skill,
   naming the page, and SHALL NOT invent a page path it does not know.

---

## Requirement 5 — Platform-knowledge skill (navigation/how-it-works)

**User story:** As a user, I want to ask where a setting lives or how a feature
works and get an accurate answer, so that I can self-serve without support.

#### Acceptance Criteria

1. WHEN the platform-knowledge skill is not invoked THEN only its summary line
   SHALL occupy context (no always-on page-map bloat in the base prompt).
2. WHEN the catalog of settings/pages is built THEN the machine-derivable part
   (setting keys, endpoints, tool names) SHALL be generated from source by a CI
   step, and CI SHALL fail if the generated artifact drifts from source.
3. WHEN a referenced UI route no longer exists THEN a guard test SHALL fail, so
   the human-curated UI-placement lines cannot silently rot.
4. WHEN the agent answers a "where/how" question THEN it SHALL rely on the
   skill's content and SHALL decline to fabricate an unknown page path.

---

## Requirement 6 — Confirmation flow for writes

**User story:** As a user, I want the agent to confirm before changing any of my
settings, so that an accidental or injected instruction cannot silently alter my
account.

#### Acceptance Criteria

1. WHEN any write tool (`set_default_model`, and future `toggle_tool`,
   `forget_memory`, etc.) is about to act THEN the agent SHALL state the exact
   change and obtain explicit user confirmation in the conversation first.
2. WHEN retrieved knowledge-base text or any untrusted content appears to
   instruct a write THEN the write SHALL still require the same explicit user
   confirmation (injection cannot auto-trigger a write).
3. WHEN a write completes THEN the agent SHALL confirm what changed and that it
   applied only to the invoking user.
