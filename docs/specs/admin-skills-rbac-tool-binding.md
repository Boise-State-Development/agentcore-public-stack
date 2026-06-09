# Admin-Managed Skills with RBAC + Tool Binding

**Status:** Design / Plan
**Author:** (drafted with Claude)
**Date:** 2026-06-08
**Targets branch:** `develop`

## 1. Summary

Add an **admin feature to author "Skills"** — instruction bundles (SKILL.md body) that **bind a curated set of existing catalog tools** — and make those skills **available to user roles via RBAC**, exactly mirroring how tools are gated today.

This leverages the already-implemented progressive-disclosure runtime (`SkillAgent` + `SkillRegistry` + `skill_dispatcher`/`skill_executor`, in `backend/src/agents/main_agent/skills/`), which replaces N individual tool schemas with 2 meta-tools and injects a lightweight skill catalog into the system prompt. Today that system is **file-based** (`definitions/*/SKILL.md`) and can only bind **local decorated tools**. This work makes skills **DynamoDB-backed, admin-authored, RBAC-gated, and able to bind tools across all four sources** (local / Gateway MCP / external MCP / A2A).

The cost-effectiveness payoff (the original motivation): a role's tools get folded behind two meta-tools and a short catalog, instead of every tool schema riding in context every turn. See `docs/specs/` siblings and the memory notes `project_tool_search_token_bloat_strategy` and `project_skills_registry_tool_binding`.

### Goals
- Admins can **create / edit / delete skills** (id, name, description, instructions, bound tools, status) from the admin UI, mirroring the Tools admin feature.
- A skill **binds existing catalog tools** by selecting them (multi-select picker), across all protocols.
- Skills are **granted to AppRoles** (RBAC), exactly like tools (`granted_skills` on `AppRole`).
- The runtime loads **only the skills a user's roles grant**, folds their bound tools behind the meta-tools, and lists the skill catalog in the prompt.
- **Don't code into a corner**: the data model reserves ownership/visibility fields so a future "users author & share their own skills" phase layers on cleanly.

### Non-goals (this phase)
- User-authored or user-shared skills (Phase 2 — designed for, not built).
- AgentCore **Registry** as the backing store (DynamoDB now; Registry is a later governance/discovery option — see `project_skills_registry_tool_binding`).
- Binding the built-in **factory/context tools** (code-interpreter, artifacts, spreadsheet) — those stay always-on/context-bound. Skills bind **catalog** tools only in v1.
- A skill "discover" endpoint (skills are authored, not discovered — unlike MCP tools).

## 2. Background & the limitation we're removing

| Component | File | Role |
|---|---|---|
| `SkillAgent._create_agent()` | `backend/src/agents/main_agent/skill_agent.py:51` | Builds tools → discovers skills → binds → swaps skill-bound tools for `skill_dispatcher`+`skill_executor` → injects catalog. Falls back to plain `ChatAgent` when 0 skills (`skill_agent.py:61`). |
| `SkillRegistry` | `backend/src/agents/main_agent/skills/skill_registry.py` | File scan (`discover_skills`, L63) + `bind_tools` (L106) matching the `_skill_name` attribute. 3 levels: catalog → instructions → tools. |
| meta-tools | `backend/src/agents/main_agent/skills/skill_tools.py` | `skill_dispatcher` (load a skill's instructions + tool schemas) / `skill_executor` (run a tool within an active skill). |
| selection | `backend/src/apis/inference_api/chat/service.py:202` | `agent_type` defaults to `"chat"`; `"skill"` is **opt-in** and effectively dark today. |

**The limitation:** `bind_tools` matches `tool_obj._skill_name`, an attribute stamped by the `@skill()` decorator on **local Python tools only**. Gateway/external/A2A tools are MCP-client / dynamic objects with no `_skill_name`, so **they can't be folded into skills today**. Admin skills must bind tools by **catalog `tool_id`** and resolve those to live objects across all four sources at bind time — that resolver is the core new runtime work.

## 3. Key design decisions

1. **RBAC model = mirror tools (role → skills).** Add `skills` to `EffectivePermissions` and `granted_skills` to `AppRole`, plus a reverse-lookup GSI and `/admin/skills/{id}/roles` endpoints — identical shape to tools. Keeps the RBAC engine uniform (`resolve_user_permissions` already unions per type) and reuses the existing role-assignment UI pattern. *Rejected:* a `skill.allowed_roles` list on the record — diverges from the tool pattern and complicates `resolve_user_permissions`.

2. **A skill is a capability grant.** If a user's role grants a skill, the skill's bound tools become available to that user **when the skill is active**, independent of per-tool `enabled_tools`. The admin curates the binding; the skill carries the authorization. Concretely, the agent's tool universe becomes `(enabled_tools ∩ RBAC tools) ∪ (bound_tool_ids of RBAC-granted skills)`, and the skill-bound subset is folded behind the meta-tools. This is what makes skills a token-cheap *bundle* rather than just a label.

3. **Reuse the `app-roles` DynamoDB table.** It already holds `TOOL#`, `ROLE#`, and user-prefs items. Skills use `PK = SKILL#{skill_id}`, `SK = METADATA`. One table, consistent with tools, no new construct beyond a GSI. (Add the owner GSI now — see §5 — so Phase 2 needs no migration.)

4. **DB-backed is primary; keep the file loader as a dev/seed path.** `SkillRegistry` gains a repository-backed source; the existing `definitions/*/SKILL.md` scan stays available for local dev and is the seed source for the two example skills. Bootstrap seeds them into DynamoDB.

5. **Make `agent_type="skill"` the default once DB-backed.** `SkillAgent` already degrades to `ChatAgent` behavior when a user has 0 accessible skills (`skill_agent.py:61`). So defaulting to `"skill"` is safe: users with no granted skills are unaffected; users with granted skills get the folded, cheaper context. *Recommendation, not required for the MVP* — can stay opt-in until validated.

6. **Resolve bound tools at the `SkillAgent` layer, not deep in `SkillRegistry`.** `_build_filtered_tools()` already owns the cross-source expansion (incl. `expand_gateway_tool_ids` for the `gateway_<target>___<tool>` runtime form vs. the bare catalog id). `SkillAgent` builds a `{catalog_tool_id → live tool object}` map there and hands it to `bind_tools`, so the resolver reuses existing expansion logic instead of re-implementing it.

## 4. Data model

### 4.1 `SkillDefinition` — `apis/shared/skills/models.py` (new, mirrors `ToolDefinition`)

```python
class SkillStatus(str, Enum):
    ACTIVE = "active"; DRAFT = "draft"; DISABLED = "disabled"

class SkillVisibility(str, Enum):      # reserved for Phase 2; v1 always ADMIN
    ADMIN = "admin"; PRIVATE = "private"; SHARED = "shared"

class SkillDefinition(BaseModel):
    skill_id: str                      # ^[a-z][a-z0-9_]{2,49}$  (same regex as tool_id)
    display_name: str
    description: str                   # Level-1 catalog line (token-cheap)
    instructions: str                  # Level-2 SKILL.md body, loaded on dispatch
    bound_tool_ids: List[str] = []     # catalog tool_ids, span all protocols
    compose: List[str] = []            # composite skills (existing concept)
    status: SkillStatus = SkillStatus.ACTIVE
    category: Optional[str] = None      # optional grouping (reuse ToolCategory-like enum)

    # --- forward-compat (reserved; enforced ADMIN-only in v1) ---
    owner_id: str = "system"           # future: user_id of author
    visibility: SkillVisibility = SkillVisibility.ADMIN

    # --- display-only, computed from RBAC (like ToolDefinition.allowed_app_roles) ---
    allowed_app_roles: List[str] = []

    # --- audit ---
    created_at: datetime; updated_at: datetime
    created_by: Optional[str] = None; updated_by: Optional[str] = None
    # to_dynamo_item() / from_dynamo_item()  — mirror tools
```

### 4.2 RBAC extensions — `apis/shared/rbac/models.py`

```python
@dataclass
class EffectivePermissions:
    tools: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)   # NEW
    quota_tier: Optional[str] = None

@dataclass
class AppRole:
    ...
    granted_skills: List[str] = field(default_factory=list)   # NEW (mirror granted_tools)
```

`_compute_effective_permissions` (admin_service.py) unions `granted_skills` across inheritance, identical to tools/models. `AppRoleCreate/Update/Response` (rbac/models.py) gain `grantedSkills` / `skills`. **No change to the auth chain** — `require_admin` / `resolve_user_permissions` are already generic over permission types.

## 5. Persistence — `apis/shared/skills/repository.py` (new, mirrors `ToolCatalogRepository`)

- **Table:** reuse `DYNAMODB_APP_ROLES_TABLE_NAME` (the `app-roles` table).
- **Keys:** `PK = SKILL#{skill_id}`, `SK = METADATA`. List via `scan` with `begins_with(PK, "SKILL#")` (same as tools).
- **New GSI (add now for Phase 2):** `SkillOwnerIndex` — `GSI4PK = OWNER#{owner_id}`, `GSI4SK = SKILL#{skill_id}`. v1 admin lists don't need it, but provisioning it now avoids a later table migration when users own skills. (CDK: `infrastructure/lib/constructs/data/auth-tables-construct.ts`, alongside the existing tool/role GSIs.)
- **Role→skill reverse lookup:** mirror the tool pattern — write `SKILL_GRANT#{skill_id}` items per role with `GSI`-indexed reverse mapping so `/admin/skills/{id}/roles` can answer "which roles grant this skill" (the tools path uses `ToolRoleMappingIndex`; reuse the same GSI keyspace with a `SKILL#` partition value).
- **Methods:** `get_skill`, `list_skills(status?)`, `create_skill`, `update_skill`, `soft_delete_skill`, `skill_exists`, `batch_get_skills`.
- **Freshness:** mirror `apis/shared/tools/freshness.py` (10s TTL, `invalidate(skill_id)` on admin write).

## 6. Backend API — `app_api/admin/skills/`

Mirror `app_api/admin/tools/routes.py`; all routes `Depends(require_admin)`. Service `SkillCatalogService` in `app_api/skills/service.py` (mirrors `ToolCatalogService`), depends on `SkillCatalogRepository` + `AppRoleService`/`AppRoleAdminService`.

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/skills` | list (optional `status` filter); hydrate `allowedAppRoles` |
| GET | `/admin/skills/{skill_id}` | one skill + roles |
| POST | `/admin/skills` | create; **validate every `bound_tool_id` exists in the tool catalog** and is ACTIVE; reject unknown/disabled |
| PUT | `/admin/skills/{skill_id}` | update; re-validate bound tools |
| DELETE | `/admin/skills/{skill_id}?hard={bool}` | soft (disable) / hard |
| GET | `/admin/skills/{skill_id}/roles` | roles granting this skill (`direct`/`inherited`) |
| PUT | `/admin/skills/{skill_id}/roles` | replace grants (bidirectional sync) |
| POST | `/admin/skills/{skill_id}/roles/add` · `/remove` | delta grants |

No `/discover`. The create/edit form populates the tool picker from the existing `GET /admin/tools` list.

**Boundary compliance:** admin endpoints under `app_api/admin/skills/` (CLAUDE.md); shared models/repo under `apis/shared/skills/` so both `app_api` (admin) and the runtime (`agents`/`inference_api`) consume them without crossing the import boundary (`tests/architecture/test_import_boundaries.py`). User auth on any non-admin skill route (Phase 2) must use `get_current_user_from_session`.

## 7. RBAC enforcement (server-side)

Add a `SkillAccessService` in `app_api/admin/services/skill_access.py` mirroring `tool_access.py`:
- `can_access_skill(user, skill_id)` / `get_user_allowed_skills(user)` → from `resolve_user_permissions(user).skills` (with `"*"` wildcard).
- `filter_allowed_skills(user, requested)` → intersect.

The runtime never trusts a client-supplied skill list: the set of active skills is computed server-side from the user's resolved permissions when the agent is built (§8). Admin writes bump the existing `roles_version` watermark + skill freshness so caches invalidate.

## 8. Runtime integration

### 8.1 Load role-filtered skills
- Thread `user_roles` (or, better, the **resolved accessible skill ids**) into `inference_api/chat/service.py::get_agent` (currently not passed) and on to the `SkillAgent` constructor. `current_user.roles` is available at the route (`chat/routes.py`).
- `SkillRegistry` gains a repository source: `discover_skills()` becomes pluggable — file scan **or** `SkillCatalogRepository` filtered to the user's allowed skill ids (+ ACTIVE). Internal `_skills[name]` dict gains `bound_tool_ids` and stores the `instructions` body (so `load_instructions` reads from the record, not a file).

### 8.2 Resolve bound tools across all 4 sources (the core new work)
- In `SkillAgent._create_agent()`: when building the tool universe, **augment `enabled_tools` with the bound_tool_ids of the user's accessible skills** (subject to the skill being RBAC-granted), so `_build_filtered_tools()` materializes those live objects.
- Build a `{catalog_tool_id → live_tool_object}` map during/after `_build_filtered_tools()`, reusing `expand_gateway_tool_ids` (gateway runtime ids are `gateway_<target>___<tool>`; the catalog stores the bare id) and the external/A2A identifiers (`.tool_name` / `.name`).
- Pass that map to `bind_tools` so a skill's `bound_tool_ids` resolve to objects regardless of source — removing the local-only `_skill_name` limitation. (Keep `_skill_name` matching as a fallback for decorated local tools.)
- Unbound / non-skill tools behave exactly as today.

### 8.3 Cache correctness
- Add a `skills_hash` to `_create_cache_key` (`inference_api/chat/service.py:49`) = hash of `sorted(accessible_skill_ids)` + their `updated_at` (mirror `freshness_hash`). Two users with the same `enabled_tools` but different roles must not share an agent. Thread `user_roles`/accessible-skill set into the key.
- Admin skill edits + role-grant changes invalidate via skill freshness + `roles_version`.

### 8.4 Default agent type (recommended, optional)
- Once DB-backed and validated, set the default `agent_type` to `"skill"` (graceful fallback to chat when 0 skills). Until then, pilot on one assistant via `agent_type="skill"`.

## 9. Frontend — `admin/skills/` (mirror `admin/tools/`)

Reuse the exact Angular patterns from `frontend/ai.client/src/app/admin/tools/`:
- **Models:** `admin-skill.model.ts` — `AdminSkill`, `AdminSkillListResponse`, `SkillRoleAssignment`, create/update requests, `SKILL_STATUSES`/`SKILL_CATEGORIES` constants.
- **Service:** `admin-skill.service.ts` (`providedIn: 'root'`, `resource()` + signals) — `fetchSkills/createSkill/updateSkill/deleteSkill/getSkillRoles/setSkillRoles`.
- **List page:** `skill-list.page.ts` — `OnPush`, search/status filters, expandable rows showing bound-tool badges + role badges, row actions (roles dialog, edit, delete). Same tokens: `rounded-2xl`, `text-sm/6`, `bg-blue-600`, dark variants (matches the redesign tokens note).
- **Form page:** `skill-form.page.ts` — reactive form: `skillId` (create-only, same regex), `displayName`, `description`, `instructions` (textarea / markdown), `status`, plus a **tool picker**.
- **Tool picker dialog:** new `tool-picker-dialog.component.ts` — reuse the multi-select checkbox UI from `tool-role-dialog.component.ts`; load options from `AdminToolService.getTools()`; search/filter by name/category/protocol; returns `string[]` of `tool_id`s.
- **Role dialog:** copy `tool-role-dialog.component.ts` → `skill-role-dialog.component.ts` (logic is generic).
- **Routes:** add `skills`, `skills/new`, `skills/edit/:skillId` in `admin/admin.routes.ts`.

Cross-package contract: the TS `AdminSkill` interface must match the backend response shape (CLAUDE.md — breaking changes update both packages in one PR).

## 10. Forward-compat: user-authored & shared skills (Phase 2, designed not built)

The model already carries `owner_id` + `visibility`. Phase 2 adds:
- User-facing routes under `apis/app_api/skills/` (auth via `get_current_user_from_session`) for create/list-own.
- A sharing layer mirroring **collaborative assistant editing** (`project_issue113_collab_editing`): `owner_id` is the access gate; viewer/editor shares grant access without changing the mutation signatures. A user's accessible skills become `RBAC-granted (admin) ∪ owned ∪ shared-with-me`.
- The `SkillOwnerIndex` GSI (provisioned in §5) serves "list my skills".
- Optional later: back the whole catalog with **AgentCore Registry** for org-wide governance/discovery (`project_skills_registry_tool_binding`) — swap the repository source behind `SkillCatalogRepository`/`SkillRegistry`, which are already the seams.

No v1 schema choices block any of this.

## 11. Phasing / PR breakdown

Sequence so each PR is independently reviewable (branch from `develop`):

1. **PR-1 — Shared model + persistence.** `apis/shared/skills/` (`models.py`, `repository.py`, `freshness.py`); CDK `SkillOwnerIndex` GSI. Unit tests. No behavior change.
2. **PR-2 — RBAC extension.** `granted_skills` / `EffectivePermissions.skills`, effective-permission computation, role-assignment reverse lookup, `SkillAccessService`. Tests for resolution + wildcard + inheritance.
3. **PR-3 — Admin API.** `app_api/admin/skills/routes.py` + `app_api/skills/service.py` (CRUD + role endpoints + bound-tool validation).
4. **PR-4 — Admin frontend.** `admin/skills/` list + form + tool-picker dialog + role dialog + routes.
5. **PR-5 — Runtime wiring.** `SkillRegistry` repo source; cross-source `bind_tools` resolver; thread `user_roles`/accessible-skills + `skills_hash` through `get_agent`; bootstrap seed of the two example skills.
6. **PR-6 — Default/rollout (optional).** Flip default `agent_type` to `"skill"` (or pilot on one assistant); measure `toolTokens` before/after via the existing context-attribution partition.

## 12. Risks / open questions

- **Cross-source resolver correctness** (gateway expanded ids, external `.tool_name`, A2A) — the highest-risk piece; cover with runtime tests binding one tool of each protocol.
- **Cache key completeness** — missing `skills_hash`/roles would cross-pollute agents between users; assert in tests.
- **Skill-as-grant semantics** — confirm the policy that a granted skill authorizes its bound tools even when not individually enabled. (Recommended; admin is trusted. Flag for product sign-off.)
- **`pytest` is the only correctness gate** (not in CI per `project_pytest_not_in_ci`) — run the full local suite for PR-1/2/5 (shared + RBAC + runtime).
- **Composite skills** (`compose`) interacting with role grants — a composed child skill's tools should still require the parent to be granted; verify `get_tools` aggregation respects this.

## 13. Definition of done (Phase 1)
An admin can create a skill with bound tools and grant it to a role; a user in that role gets a SkillAgent whose context shows the skill catalog + 2 meta-tools (not the bound tools' raw schemas), can activate the skill, and invoke its tools — across local, gateway, and external sources — with the `toolTokens` partition measurably lower than the equivalent all-tools-enabled ChatAgent.
