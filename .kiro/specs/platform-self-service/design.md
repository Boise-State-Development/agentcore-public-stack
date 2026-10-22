# Platform Self-Service — Design

## Overview

Two orthogonal capabilities — **knowledge** (how the platform works / where
things are) and **account access** (read + confirmed-write of the user's own
state) — delivered through a new **system tier** for tools and skills so the
platform-shipped ones are always-on, user-untoggleable, and hidden from settings
while still visible in the session transcript.

Design principle carried from the discussion: **nothing grows the always-on base
prompt.** Knowledge lives in a progressive-disclosure skill; account facts live
in on-demand tools. A turn about writing an essay pays for none of it.

This design leans on primitives that ALREADY exist in the repo rather than
building parallel machinery. The three new things are: a `system`/`hidden` tool
classification, an `internal` skill flag, and the tools themselves.

---

## Architecture

```
User question
     │
     ▼
AgentCore Runtime turn (inference_api/chat/routes.py)
     │
     ├─ base system prompt  ── one-line pointer only (no page-map, no quota)
     │
     ├─ skills (progressive) ── platform-knowledge skill: summary line in ctx,
     │                          full page/settings catalog loads on invoke
     │
     └─ tools (effective set) ── system tools force-merged always-on
                                 (reuses resolve_always_on_tool_ids)
                                   │
                    ┌──────────────┼───────────────┐
                    ▼              ▼                ▼
             get_my_quota   get_my_settings     whoami        ... (read)
             set_default_model (write · confirm)               ... (write)
                    │
                    ▼  identity from request context (NOT an arg)
             existing user-scoped services:
               QuotaChecker · user_settings repo · users repo · tool prefs
```

Two surfaces stay deliberately separate:

| Surface | Code path | System tool behavior |
|---|---|---|
| Session transcript tool-use event | `streaming/stream_processor.py` emits `tool_use{name,input}` | **Shown** — fires for any invoked tool |
| Settings Tools panel toggle list | `GET /tools/` → `app_api/tools/service.py::get_user_accessible_tools` | **Hidden** — filtered out by `hidden` flag |

---

## Component 1 — System / hidden tool tier

### Existing primitives (reuse, do not reinvent)

- `ToolDefinition.always_on` in `backend/src/apis/shared/tools/models.py` — an
  admin pin that forces a tool into the effective set; user prefs cannot remove
  it.
- `resolve_always_on_tool_ids(user)` in
  `backend/src/apis/shared/tools/always_on.py`, force-merged every turn in
  `inference_api/chat/routes.py::_apply_admin_always_on_tools` and in
  `voice_routes.py`. Behind `ADMIN_ALWAYS_ON_TOOLS_ENABLED`.
- Freshness snapshot `get_always_on_tool_ids()` in
  `backend/src/apis/shared/tools/freshness.py`.

### New fields on the tool model

Add to `ToolDefinition` (and mirror in `ToolMetadata` catalog entries):

| Field | Type | Meaning |
|---|---|---|
| `system` | bool (default `false`) | Provenance: shipped with the app, not an admin knob. Implies always-on semantics without an admin having to pin it per deployment. |
| `hidden` | bool (default `false`) | Excluded from the `GET /tools/` toggle list, but retained (flagged) in catalog payloads used for display labeling. |

Both default false → every existing tool row is unchanged (Req 1.6).

### Effective-set resolution

- A `system` tool is added to the effective tool set for any user whose RBAC
  grants it, via the same union path as `always_on` — extend
  `resolve_always_on_tool_ids` (or a sibling `resolve_system_tool_ids`) so
  system ids are always included regardless of the admin `always_on` table and
  regardless of the `ADMIN_ALWAYS_ON_TOOLS_ENABLED` flag (system is not an admin
  toggle).

### Panel hiding without breaking transcript labels (Req 1.2–1.4)

- `get_user_accessible_tools` gains a filter: exclude `hidden` tools from the
  list the settings panel consumes.
- BUT the catalog payload the frontend uses to **label** `tool_use` events keeps
  hidden tools, each flagged `hidden: true`. Frontend rule:
  - toggle list = entries where `!hidden`
  - label map = all entries (including hidden) → renders "Looking up your
    account info" + icon for a `whoami` tool-use event.
- The streaming path is untouched: `stream_processor.py` already emits
  `tool_use` with the tool `name` for any invoked tool, so in-session visibility
  is free.

---

## Component 2 — Internal skill tier

### Existing primitives

- Skill provenance: catalog skills are `owner_id == "system"`; user skills are
  private/user-owned (`app_api/skills/service.py`).
- Access resolves as "RBAC-granted catalog ∪ own"
  (`apis/shared/skills/access.py::resolve_accessible_skill_ids`).
- `skill://` resources load only a summary line until invoked (progressive
  disclosure).

### New

- An `internal` marker on system-owned skills that:
  - removes them from the user's "My Skills" management UI,
  - makes them non-editable / non-disable-able by users,
  - includes them in the invocable set for any user whose role grants them,
    without a per-turn `enabled_skills` opt-in (Req 2.3).
- Implementation seam: `resolve_accessible_skill_ids` / the agent-binding
  resolver treat `internal` skills as always-in-scope for granted roles, and the
  user-facing skill list service filters `internal` out.

---

## Component 3 — Account tools (identity binding)

### Identity from context, never an argument (Req 3)

`@tool` functions in `backend/src/agents/local_tools/` receive their arguments
from the model. Identity must NOT be one of them. Bind `user_id`/`User` the same
way OAuth tools resolve it in
`backend/src/apis/inference_api/chat/app_tool_dispatch.py` — from the
request/agent context established when the agent is built for the turn.

Options considered:
- **A contextvar / agent-state carried user** (preferred): set the invoking
  `User` on a request-scoped contextvar when the agent is created for the turn;
  account tools read it. Matches how the turn already threads `user_id`,
  `auth_token`.
- Reject: passing `user_id` as a tool parameter (model-controllable → cross-user
  risk).

### Tool registration

Local tools auto-register via `register_module_tools(local_tools)` in
`backend/src/agents/main_agent/tools/tool_registry.py` (walks `__all__`). New
tools land in `backend/src/agents/local_tools/`, are exported in `__all__`, and
get catalog metadata in
`backend/src/agents/main_agent/tools/tool_catalog.py` with `system=true`,
`hidden` per the visibility rule below.

### Skill 1 — Account & Usage

| Tool | R/W | Backing | Visibility |
|---|---|---|---|
| `get_my_quota` | read | `QuotaChecker.check_quota(user)` (`quota/checker.py`) — already computed each turn | visible-but-locked |
| `get_my_settings` | read | user-settings repo (`apis/shared/user_settings/repository.py`) | visible-but-locked |
| `whoami` | read | users repo / session `User` | **hidden** (pure plumbing) |
| `set_default_model(model)` | write · confirm | user-settings update, validated against allowed models | visible-but-locked |

**Visibility rule (from the discussion):** pure plumbing (`whoami`) is `hidden`;
user-beneficial capabilities (`get_my_quota`, `get_my_settings`,
`set_default_model`) are `system` + visible-but-locked so users discover the
capability. All are `system` (always-on, not user-disable-able). All still emit
`tool_use` events in the transcript.

`get_my_quota` returns the same fields the enforcement path already computes:
`current_usage, quota_limit, remaining, percentage_used, tier`. No new data path.

---

## Component 4 — Platform-knowledge skill + CI generation

- A `system` + `internal` skill whose body is the page/settings catalog.
- **Machine-derivable part** (CI-generated): setting keys, endpoints, tool names
  — walked from the route definitions and settings models. A CI step regenerates
  and diffs it; drift fails the build (same pattern as the lakehouse
  "dependency diagram up to date" gate — pin file I/O to `encoding='utf-8'` per
  the repo lesson so the drift check is stable across runners).
- **Human-curated part** (UI placement, "under the gear icon"): a small
  hand-maintained section, guarded by a test that fails if a referenced Angular
  route disappears — so it cannot silently rot (Req 5.3).
- Base prompt gets **one sentence** pointing at both the knowledge skill and the
  account tools; it does not carry the catalog or any per-user data.

---

## Data model changes

- `ToolDefinition` / `ToolMetadata`: `+ system: bool`, `+ hidden: bool` (both
  default false; `from_dict` reads absent keys as false — backward compatible,
  same pattern as the existing `alwaysOn` migration).
- Skill records: `+ internal: bool` on system-owned skills.
- No new tables; no changes to the `tool_preferences` map shape (system tools
  are simply never written there).

---

## Security & safety

- Scope: every account tool is user-scoped through an existing endpoint;
  RBAC/ownership checks are reused, not reimplemented.
- Identity: request-context only (Req 3); never a model argument.
- Writes: confirmation-gated (Req 6); injection via KB text cannot auto-fire a
  write because confirmation is a human turn.
- Excluded surfaces: API-key mint/rotate, fine-tuning/inference jobs,
  share/publish, `admin/*` — never registered into any skill.
- Consistent with `PLATFORM_SAFETY_FLOOR`: identity claims come from the
  validated session, not the prompt.

---

## Testing strategy

- Unit: `system`/`hidden` defaults false; `get_user_accessible_tools` excludes
  hidden from the toggle list but retains it (flagged) in the label payload;
  system tool present in effective set regardless of user prefs / admin flag.
- Unit: account tools reject a model-supplied user id and resolve from context;
  fail closed when context identity is absent.
- Unit: `set_default_model` validates the allowed-model set and is
  confirmation-gated.
- Architecture: import-boundary tests still pass (tools live under `agents/`,
  call `apis.shared`, never `app_api`↔`inference_api`).
- CI: platform-knowledge catalog regeneration is byte-stable and drift-gated;
  route-guard test fails on a removed referenced route.
- Frontend (CI): a hidden tool does not render a toggle row but a `tool_use`
  event for it still renders a friendly label + icon.
