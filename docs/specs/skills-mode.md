# Skills Mode — user-visible mode toggle + admin policy

> Status: DRAFT (spec only, nothing built). Follows on from
> `admin-skills-rbac-tool-binding.md` Phase 1 (PR-1..7, all merged — default
> `agent_type` is already `"skill"` server-side as of #470). This spec makes the
> mode a **first-class, user-visible concept** with per-skill toggles and
> admin-controlled policy.

## 1. Summary

Two user-facing modes, surfaced in the existing model-settings slide-over:

- **Skills mode** (`agent_type="skill"`) — routes through `SkillAgent`. The panel
  shows the user's RBAC-accessible skills as a toggle list (mirroring today's tool
  toggles). Capabilities come from the enabled skills' bound tools; the raw tool
  picker is hidden. This is the tool-hiding UX that motivated skills.
- **Tools mode** (`agent_type="chat"`) — routes through plain `ChatAgent`. The
  existing fine-grained tool picker (whole-server + per-MCP-tool toggles) applies,
  unchanged.

Two admin-controlled global policies:

- **Default mode** — which mode new conversations (and clients that don't specify)
  get. Replaces the hardcoded `DEFAULT_AGENT_TYPE` constant as the source of truth.
- **Allow mode toggle** — whether users may switch modes at all. When off, the
  toggle UI is hidden *and* the server ignores a client-supplied `agent_type`
  (UI gating alone is not enforcement).

## 2. Current state (verified 2026-06-11)

What already exists and is load-bearing for this feature:

| Piece | Where | State |
|---|---|---|
| Server default agent type | `inference_api/chat/routes.py:79` (`DEFAULT_AGENT_TYPE = "skill"`) | Hardcoded constant; flipped in #470 |
| Per-turn override | `inference_api/chat/models.py:109` (`InvocationRequest.agent_type`) | Exists; SPA never sends it today |
| Effective-type resolution | `inference_api/chat/routes.py:715` (`input_data.agent_type or DEFAULT_AGENT_TYPE`) | Skills resolved only when effective type is `"skill"` (lines 722–726) |
| Resume | `PausedTurnSnapshot.agent_type` (`apis/shared/sessions/models.py:109`), restored at `routes.py:1327` | Resume already gates skill resolution on `snapshot.agent_type == "skill"` |
| Accessible skills | `_resolve_accessible_skill_ids` (`inference_api/chat/routes.py:670–689`) → `AppRoleService.get_accessible_skills` + `"*"` expansion | All-or-nothing: user always gets every RBAC-granted skill; **no per-skill user toggle exists** |
| SkillAgent | `agents/main_agent/skill_agent.py:76+` | Takes `accessible_skill_ids`; degrades to ChatAgent behavior at 0 skills |
| Agent cache key | `inference_api/chat/service.py:176–183` (`skills_hash` over ids + `updated_at`) | Hashes the *accessible* set — must hash the *effective* set once toggles exist |
| User-facing skills API | — | **None.** `app_api/skills/service.py` is the admin-backing `SkillCatalogService`; no user routes, no "list my skills" endpoint |
| Tool toggles (user) | `app_api/tools/routes.py` (`GET /tools/`, `PUT /tools/preferences`), prefs in `apis/shared/tools/repository.py`; SPA `ToolService` + Tools section of `components/model-settings/` | The exact pattern to mirror for skills |
| SPA chat request | `session/services/chat/chat-request.service.ts:194–257` | Sends `enabled_tools`, `model_id`, `inference_params`, `selected_prompt_id`; **no `agent_type`** |
| BFF chat proxy | `app_api/chat/proxy_routes.py` | Relays the body verbatim — request-shape changes touch only inference-api + SPA |
| Session preferences | `SessionPreferences` (backend `apis/shared/sessions/models.py:125`; FE `session-metadata.model.ts:14–22`) | Has `lastModel`/`selectedPromptId` hydration precedent in `session.page.ts:183–211`; no mode field |
| User settings | `apis/shared/user_settings/repository.py` (`USER#{id}` / `SETTINGS`) | Only `defaultModelId` today; natural home for `preferredAgentMode` + skill prefs |
| Global admin settings store | — | **None.** No platform-settings table, no feature-flag/bootstrap endpoint for the SPA |

## 3. Key design decisions

1. **Skills are the only capability unit in skills mode.** The SPA sends
   `enabled_tools: []` when in skills mode; active tools are exactly the bound
   tools of the enabled skills (skill-as-grant folding, unchanged from PR-6).
   *Alternative (rejected for v1): send both and union them — blurs the mental
   model and resurrects the tool-bloat the feature exists to kill. A standalone
   tool a user needs in skills mode is a signal the admin should wrap it in a
   skill.*
2. **Per-skill toggles are a global user preference**, not per-session — exactly
   mirroring tool preferences (`PUT /tools/preferences`). Mode itself is
   per-session (`SessionPreferences.agent_type`) with a user-level
   `preferredAgentMode` for new conversations — exactly mirroring model selection
   (`lastModel` + `defaultModelId`).
3. **Server-side enforcement of the toggle policy.** When `allow_mode_toggle` is
   false, inference-api overrides any client `agent_type` with the admin default.
   Enforcement applies only when the requested type is `"chat"` or `"skill"` —
   `"voice"` and any future internal types are untouched.
4. **Admin policy lives in a new `platform_settings` shared domain** stored as a
   **sentinel item in the auth-providers table** (`PK=SK=SYSTEM_SETTINGS#chat-mode`),
   following the existing `SYSTEM_SETTINGS#first-boot` convention
   (`app_api/system/repository.py`). *(Revised 2026-06-11 from the original
   dedicated-table recommendation: the first-boot precedent is the established
   house pattern for global state, and the auth-providers table's name + IAM
   read access are **already wired into both app-api and the inference runtime**
   — so this needs zero CDK changes.)* Both app-api (admin CRUD + SPA read) and
   inference-api (policy enforcement) consume it via `apis.shared` — respecting
   the import-boundary rule. Inference-api reads through a short in-process TTL
   cache (~60 s) so policy flips don't require a redeploy but also don't add a
   Dynamo read per turn.
5. **`enabled_skills` request semantics:** `None`/absent → all accessible skills
   (today's behavior, so existing clients are unaffected); a list → intersected
   server-side with the RBAC-accessible set (client input is never trusted to
   grant). Empty list → zero skills → SkillAgent's existing degrade-to-chat path.
6. **Missing-table / local-dev fallback:** when the platform-settings table or
   item is absent, policy defaults to `{default_mode: DEFAULT_AGENT_TYPE,
   allow_mode_toggle: true}` — current behavior, zero-config local dev.

## 4. Data model

### 4.1 Platform settings — `apis/shared/platform_settings/` (new)

```python
class ChatModeSettings(BaseModel):
    default_mode: Literal["skill", "chat"] = "skill"
    allow_mode_toggle: bool = True
    updated_at: datetime
    updated_by: str  # admin user id, audit trail
```

`models.py` + `repository.py` (table `{prefix}-platform-settings`, env
`DYNAMODB_PLATFORM_SETTINGS_TABLE_NAME`) + `service.py` with a TTL-cached
`get_chat_mode_settings()` read used by inference-api.

### 4.2 Request contract — `inference_api/chat/models.py`

```python
enabled_skills: Optional[List[str]] = None  # None = all accessible (back-compat)
```

### 4.3 Snapshot — `apis/shared/sessions/models.py`

Add `enabled_skills: Optional[List[str]]` to `PausedTurnSnapshot` so a paused turn
(OAuth consent) resumes with the same effective skill set. The existing
`agent_type` gate on resume stays.

### 4.4 Session preferences (backend + FE interface)

Add `agent_type: Optional[str]` to `SessionPreferences` (backend
`apis/shared/sessions/models.py:125`, FE `session-metadata.model.ts`). Per-session
`enabled_skills` is **not** stored — skill toggles are global user prefs (§3.2).

### 4.5 User settings — `apis/shared/user_settings/repository.py`

Add `preferredAgentMode: Optional[str]` alongside `defaultModelId`. Skill
preferences (`Record<skill_id, bool>`) persist via a skills-preferences store
mirroring `apis/shared/tools/repository.py`'s user-preference rows.

## 5. Backend API

### 5.1 User-facing — `app_api/skills/routes.py` (new, next to the existing service)

Auth: `Depends(get_current_user_from_session)` (house rule — no Bearer-only deps).

- `GET /skills/` — the user's RBAC-accessible **ACTIVE** skills with prefs merged:
  `{skills: [{skillId, displayName, description, category, boundToolCount,
  userEnabled, isEnabled}], appRolesApplied}`. Resolution reuses the same
  RBAC path as inference (`get_accessible_skills` + `"*"` expansion). The
  wildcard-expansion helper currently lives in `inference_api/chat/routes.py:670`
  — lift the reusable core into `apis/shared/skills/` so app-api and
  inference-api don't drift (import-boundary rule: shared by two consumers →
  `apis.shared`).
- `PUT /skills/preferences` — `{preferences: Record<skillId, boolean>}`, rejecting
  ids outside the user's accessible set (mirror `update_tool_preferences`).

### 5.2 Policy read for the SPA — `app_api/system/routes.py` (existing router)

- `GET /system/chat-settings` — `{defaultMode, allowModeToggle}` (session auth).
  Loaded once at SPA auth alongside tools/models. The `system` domain already
  hosts app-level metadata (`/system/status`, `/system/first-boot`).

### 5.3 Admin — `app_api/admin/settings/routes.py` (new domain folder)

- `GET /admin/settings/chat` / `PUT /admin/settings/chat` — `Depends(require_admin)`.
  PUT validates `default_mode ∈ {"skill","chat"}` and stamps `updated_by`.

## 6. Inference path changes — `inference_api/chat/routes.py`

1. **Policy resolution** (replaces direct `DEFAULT_AGENT_TYPE` use at line 715):

   ```python
   settings = await get_chat_mode_settings()        # TTL-cached, falls back to constant
   requested = input_data.agent_type
   if requested in ("skill", "chat") and not settings.allow_mode_toggle:
       requested = None                              # policy override, log it
   effective_agent_type = requested or settings.default_mode
   ```

   `DEFAULT_AGENT_TYPE` stays as the code-level fallback when the table is absent.

2. **Effective skill set** (extends lines 722–726): when effective type is
   `"skill"`, compute `accessible ∩ enabled_skills` (None → accessible). Pass the
   *effective* list into `service.get_agent` → `SkillAgent`.

3. **Cache key** (`service.py:176–183`): `skills_hash` now hashes the effective
   set, not the accessible set — two sessions of the same user with different
   skill toggles must not share an agent. (Known fold gotcha carries over
   unchanged: `set_folded_tool_names` must keep nulling external clients'
   pre-cached `_loaded_tools`.)

4. **Snapshot persist/restore**: `stream_coordinator.py` persists
   `enabled_skills` next to `agent_type` (lines ~1004–1016); resume threads it
   back through, still gated on `snapshot.agent_type == "skill"`.

## 7. Frontend

### 7.1 New services

- **`SkillService`** (`app/services/skill/skill.service.ts`) — mirror
  `ToolService`: `_skills` signal from `GET /skills/`, `enabledSkillIds`
  computed, `toggleSkill()` → optimistic update + `PUT /skills/preferences`.
- **`ChatModeService`** (`app/session/services/chat-mode/`) — signals:
  `policy` (from `GET /system/chat-settings`, loaded at auth), `mode`
  (`'skill' | 'chat'`), `canToggle` computed. New-session initial mode:
  `preferredAgentMode ?? policy.defaultMode`; toggling persists
  `preferredAgentMode` (user settings) and `agent_type` (session preferences).

### 7.2 Model-settings panel (`components/model-settings/`)

- **Mode control**: a two-option segmented control ("Skills" / "Tools") above the
  capabilities sections, rendered only when `canToggle()`. When toggling is
  disallowed, no control — the panel simply shows whichever section matches the
  admin default.
- **Skills section** (new, shown in skills mode): toggle list of accessible
  skills — name, description, category chip, enabled count badge in the header
  (visual twin of the Tools section). Empty state for zero accessible skills:
  "No skills are available for your role" (+ a "switch to Tools" hint when
  `canToggle()`).
- **Tools section** (existing, shown in tools mode only).

### 7.3 Chat request (`chat-request.service.ts`)

- Always send `agent_type: mode()`.
- Skills mode: `enabled_skills: skillService.getEnabledSkillIds()`,
  `enabled_tools: []`.
- Tools mode: `enabled_tools` as today; omit `enabled_skills`.

### 7.4 Session hydration (`session.page.ts`)

New effect mirroring the `lastModel` pattern: on session load, if
`preferences.agentType` is set, `chatModeService.setMode(...)` — a conversation
reopens in the mode it was created with (subject to `canToggle`; if policy now
forbids the stored mode's opposite, the policy wins).

### 7.5 Admin page

`admin/settings/` (new): a "Chat settings" page — default-mode radio
(Skills / Tools) + "Allow users to switch modes" toggle, save via
`PUT /admin/settings/chat`. Follow the list-page redesign tokens
(rounded-2xl / text-sm/6 / text-2xl/8, flat form — no heavy section cards).

## 8. Infrastructure (CDK)

**None required.** *(Revised 2026-06-11 — see §3.4.)* The chat-mode settings
item lives in the auth-providers table, whose env var
(`DYNAMODB_AUTH_PROVIDERS_TABLE_NAME`) and IAM grants already reach both the
app-api task definition (`app-api-environment.ts:254`) and the inference
runtime (`inference-agentcore-construct.ts:316`,
`inference-api-iam-roles.ts:149`). Deploy order for the rollout is just
`backend.yml` → `frontend-deploy.yml`. Backward-safe at every step: backend
tolerates a missing item (§3.6); old SPA omits the new fields (back-compat
semantics §3.5).

## 9. Edge cases

- **Zero accessible skills, skills mode** — existing SkillAgent degrade path;
  panel shows the empty state. No behavior change from today.
- **All skills toggled off** — effective set empty → degrade path; with
  `enabled_tools: []` the turn is model-only. The Skills section header's count
  badge makes this visible; acceptable v1 (matches a user disabling every tool
  in tools mode today).
- **Voice** — `agent_type="voice"` bypasses the policy override (§3.3).
- **Assistants (`rag_assistant_id`)** — out of scope; assistant turns keep their
  current behavior (they already replace `enabled_tools` with `[]`). Interaction
  between assistants and skills mode is a follow-up decision.
- **API-key / external clients** — policy enforcement is uniform (they hit the
  same `/invocations` resolution). If a service integration must pin a mode, that
  becomes an explicit carve-out later, not a silent bypass now.
- **Stale SPA policy** — SPA loads policy at auth; if an admin flips it mid-session
  the server still enforces (§3.3), so the worst case is a hidden-but-ignored
  toggle until refresh.
- **Mid-conversation mode switch** — allowed (it's per-turn server-side already);
  the per-session preference just tracks the latest choice. Resume of a paused
  turn uses the snapshot, so an in-flight turn can't change modes midway.

## 10. Testing

- **Backend (pytest — full local suite; not in CI):**
  - `platform_settings` repo/service: missing table fallback, TTL cache, PUT validation.
  - Enforcement matrix: {toggle allowed, denied} × {requested skill, chat, voice, absent}.
  - Effective-skill intersection: None → all; subset; ids outside accessible set dropped; empty → degrade.
  - Snapshot round-trip with `enabled_skills`; resume gating unchanged.
  - Cache-key differentiation on differing effective sets.
  - `GET /skills/` RBAC filtering + ACTIVE-only; preferences PUT rejects inaccessible ids.
  - Import-boundary test picks up the new shared domain automatically.
- **Frontend (`ng test`, not raw vitest; DI tokens over `vi.mock`):**
  - `SkillService` load/toggle/persist; `ChatModeService` init precedence
    (session pref > user pref > admin default) and `canToggle` gating.
  - Model-settings: section visibility per mode × policy; empty state.
  - `buildChatRequestObject` field matrix per mode.
- **Infra:** construct unit test for the table + env threading (`npx jest`).

## 11. Phasing / PR breakdown

All PRs branch from `develop`; conventional commits.

1. **PR-1 — Platform settings foundation (backend only, no CDK).**
   `apis/shared/platform_settings/` (sentinel item in the auth-providers table,
   §3.4) + `/admin/settings/chat` + `GET /system/chat-settings`. No behavior
   change (nothing consumes the policy yet).
2. **PR-2 — User skills surface + inference enforcement (backend).**
   `GET /skills/` + `PUT /skills/preferences` (incl. lifting the shared
   resolution helper into `apis/shared/skills/`), `enabled_skills` on
   `InvocationRequest`, effective-set intersection, cache-key + snapshot changes,
   policy enforcement in the effective-type resolution. Back-compat: absent
   fields reproduce today's behavior exactly.
3. **PR-3 — SPA: skills mode UX.** `SkillService`, `ChatModeService`,
   model-settings mode control + Skills section, chat-request wiring, session
   hydration, user-settings `preferredAgentMode`.
4. **PR-4 — SPA: admin chat-settings page** (+ any polish: empty states, count
   badges). Small; can fold into PR-3 if it stays lean.

## 12. Risks / open questions

- **`enabled_tools: []` in skills mode** (§3.1) — needs product sign-off: users
  who relied on ad-hoc standalone tools lose them while in skills mode until an
  admin binds them into a skill. The toggle back to tools mode (where allowed) is
  the escape hatch.
- **Policy cache window** — a ~60 s TTL means an admin lockdown takes up to a
  minute to bite on in-flight containers. Acceptable; document it on the admin page.
- **Effective-set cache-key churn** — per-user toggle changes now create more
  distinct agent cache entries; bounded by (users × toggle combinations actually
  used), same order as `enabled_tools` churn today.
- **Assistants × skills mode** deferred (§9) — revisit when assistants get a
  skills story.
- **Default-mode flip vs. existing constant** — after PR-2, `DEFAULT_AGENT_TYPE`
  is only the no-table fallback; the admin setting is authoritative. Make sure
  nightly/env bootstrap seeds the settings item so environments don't silently
  diverge from what admins see in the UI.

## 13. Definition of done

An admin can set the platform default mode and disallow switching; a user in
skills mode sees exactly their RBAC-granted active skills in the model-settings
panel, toggles a subset, and the agent's capabilities are precisely the bound
tools of the enabled skills (verified via the `contextBreakdown` tools
partition); switching to tools mode (where allowed) restores today's
fine-grained tool picker behavior; resume, paused-turn OAuth flows, voice, and
assistants behave exactly as before; old clients (no new fields) see zero
behavior change.
