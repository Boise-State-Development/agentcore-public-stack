# Granular Admin Permissions (Delegated Admin Scopes)

**Status:** Design / Proposal
**Author:** (drafted with Claude)
**Date:** 2026-07-27
**Targets branch:** `develop`

---

## 1. Problem

Admin access is a single bit. `require_admin` is literally:

```python
# apis/shared/auth/rbac.py:75
require_admin = require_app_roles("system_admin")
```

That one dependency guards **112 route handlers** across 15 admin feature areas
(costs, quota, models, tools, skills, connectors/OAuth, file sources, export
targets, marketplace, system prompts, user menu links, fine-tuning, users, roles,
auth providers). On the SPA side the gate is equally coarse: `adminGuard` sits on
the `/admin` parent route only (`app.routes.ts:31-36`); **no child route declares
its own guard**, and `admin.layout.ts` renders all five nav groups unconditionally.

So today, letting someone curate the tool catalog also lets them rewrite every
AppRole, reconfigure the IdP, and read the full cost ledger. We want a system
admin to be able to say "you can manage Skills and the Agent Marketplace, and
nothing else."

Because this is *the feature that hands out admin power*, the design has to be
led by the escalation analysis, not by the plumbing.

---

## 2. Non-goals

- **Not** replacing `system_admin`. It stays the superuser and implicitly holds
  every scope. Existing behavior must be byte-identical after this lands.
- **Not** per-record/ownership scoping ("you may edit *these three* tools").
  Scopes are per feature *area*. Record-level delegation is a separate feature.
- **Not** a new identity store. Delegation rides the existing AppRole record.
- **Not** delegating role administration itself — see §3.

---

## 3. Threat model and invariants

The whole point of being careful here: a delegated admin must not be able to
climb back to full admin. Four invariants, in priority order.

### I1 — Two admin surfaces are permanently non-delegable

`admin/roles/` and `admin/auth_providers/` stay `system_admin`-only, forever.

- **`roles`** is self-evident: whoever can `PATCH /admin/roles/{id}` can add
  scopes (or `grantedTools: ["*"]`) to a role they hold.
- **`auth_providers`** is the non-obvious one. Role resolution starts from JWT
  claims (`resolve_user_permissions` walks `user.roles` → `jwtRoleMappings`).
  Whoever controls IdP/claim configuration controls which groups arrive, and
  therefore which AppRoles resolve. It is role administration by another route.

This is encoded, not just documented: `NON_DELEGABLE_SCOPES` in the registry,
and `grantedAdminScopes` validation **rejects** any non-delegable scope at the
service layer — mirroring how `validate_jwt_role_mappings` already guards
`PROTECTED_ROLE_IDS` (`role_constraints.py:24`) so the rule holds for the REST
API, scripts, and future automation alike.

### I2 — Granting a scope is itself a `system_admin`-only act

This falls out of I1 for free, and it is the main reason to put scopes **on the
AppRole record** rather than invent a parallel "admin grants" store: the only
surface that can write `grantedAdminScopes` is the roles admin, which is
non-delegable. One lock, not two.

It also keeps us inside the rule already in `CLAUDE.md`: *the AppRole record is
the source of truth.*

### I3 — A scope must not confer capabilities from a neighboring surface

The live instance of this: the tool, model, and skill admin pages offer a "which
roles can use this?" picker that, per the RBAC contract, writes **through** into
each role's `granted*` list — `set_roles_for_tool` (`app_api/tools/service.py:636`),
`set_roles_for_model` (`admin/services/model_roles.py:152`), `set_roles_for_skill`
(`app_api/skills/service.py:523`).

That means a delegated tools-admin can mutate role records from the tools page.
Granting tools *is* the job of a tools admin, so we don't forbid it — but we
constrain the blast radius:

> **Write-through from a resource surface may not modify a role that is in
> `PROTECTED_ROLE_IDS` or that carries any `grantedAdminScopes`, unless the
> actor holds `system_admin`.**

A delegated admin can hand a tool to `faculty`. They cannot touch an
admin-bearing role at all. (Note the two role dialogs already filter
`system_admin` out of the picker client-side —
`tool-role-dialog.component.ts:220`, `skill-role-dialog.component.ts:194` — but
that is cosmetic; this invariant is the server-side version.)

### I4 — Admin scopes are granted explicitly, never inherited

`_compute_effective_permissions` (`admin_service.py:276`) merges a parent role's
`granted_tools`/`granted_models`/`granted_skills` into the child. **Admin scopes
deliberately do not participate.** Inheriting resource grants is a convenience;
silently inheriting administrative power because someone set `inheritsFrom` is
exactly the kind of surprise this feature exists to prevent. `effective.admin_scopes`
== the role's own `granted_admin_scopes`, always.

### I5 — Fail closed, and no implicit widening

`require_admin_scope` keeps the `try/except → deny` shape of `require_app_roles`
(`rbac.py:52`). There is **no `"*"` wildcard for scopes** — the wildcard idiom
used for tools/models/skills is exactly the shorthand that would let a role
accidentally become a superuser. Full admin is spelled `system_admin`.

---

## 4. Data model

A fourth grant axis on `AppRole`, alongside the existing three:

```python
# apis/shared/rbac/models.py
@dataclass
class AppRole:
    ...
    granted_tools: List[str] = field(default_factory=list)
    granted_models: List[str] = field(default_factory=list)
    granted_skills: List[str] = field(default_factory=list)
    granted_admin_scopes: List[str] = field(default_factory=list)   # NEW
```

Persisted as `grantedAdminScopes`; `from_dict` defaults to `[]` so roles written
before this feature keep working and pick scopes up on their next save — the same
rollout shape `granted_skills` used (see the `EffectivePermissions.from_dict`
docstring).

`EffectivePermissions.admin_scopes` and `UserEffectivePermissions.admin_scopes`
follow. In `_merge_permissions` (`service.py:147`) scopes **union** across a
user's roles, and — like every other list there — must be `sorted()` before it is
returned. Two of these lists already reach the model's system prompt and
`toolConfig`; keeping the whole struct deterministic is cheaper than reasoning
about which fields escaped.

### 4.1 Persistence — an attribute, not grant items

`AppRole` grants are *not* all stored the same way. `repository._build_role_items`
(`repository.py:345-423`) explodes tools/models/skills into **separate DynamoDB
items** (`TOOL_GRANT#{id}` with `GSI2PK=TOOL#{id}`, etc.) purely to support reverse
lookup — "which roles grant this tool?" — for the resource admin pages.

`grantedAdminScopes` should instead be a **plain list attribute on the
`DEFINITION` item**. Reasons:

- There is no resource page that needs the reverse lookup. The one plausible
  query ("which roles hold `admin.tools`?") is answerable from `list_roles`,
  which is already a `SK = "DEFINITION"` table scan over a small collection.
- `_delete_mapping_items` (`repository.py:425-457`) deletes mapping items **by
  prefix**, with a hand-maintained exclusion for `AGENT_PIN#` and a docstring
  explaining that a naive "everything that isn't DEFINITION" delete wipes pins on
  every role edit. Adding a fourth grant prefix means adding a fourth chance to
  get that wrong, on the axis where getting it wrong silently revokes or retains
  admin power.
- No new GSI, so no CDK change and none of the deploy-ordering hazards that come
  with adding an index.

### 4.2 Cache propagation — a security-relevant delay

`AppRoleCache` is **in-process, per ECS task**; `bump_roles_version()` is a
per-task counter, explicitly not distributed. So revoking a scope propagates to
other tasks only as their 5-minute user-permission TTL expires.

For a tool grant that is fine. For *admin* revocation it means a removed
delegated admin keeps their console for up to five minutes on tasks that didn't
serve the mutation. That is almost certainly acceptable — it is the same window
that already applies to removing someone from `system_admin` — but it should be a
stated, accepted property rather than something discovered later.

### 4.3 One guard we get for free

`admin_service.update_role` (`admin_service.py:133-146`) already strips every
field except `display_name`/`description`/`jwt_role_mappings` when the target is
`system_admin`. So `grantedAdminScopes` cannot be written onto `system_admin`
even by a superuser — which is correct, since it holds every scope implicitly.

---

## 5. The scope registry

New module `apis/shared/rbac/admin_scopes.py` — a **closed, code-defined**
registry. Not free text, not derived from a catalog.

This is the single most important structural decision, and it is forced by a
documented failure in this repo. From the `resolve_user_permissions` docstring
(`service.py:58-65`):

> the admin roles UI builds its `grantedTools` control from the tool catalog
> (`role-form.page.ts`, `availableTools()`) with no free-text entry. Anything
> that is not a catalog tool — a feature-capability id, say — therefore cannot be
> granted from the UI at all, only by hand-writing DynamoDB items. That is what
> made the short-lived `skills` capability gate inoperable and got it removed.

So admin scopes get their own first-class control fed by their own registry
endpoint. Reusing `grantedTools` would reproduce a bug we already paid for twice
(`skills`, and `scheduled-runs` — see the comment block at `sidenav.html:35-57`).

```python
@dataclass(frozen=True)
class AdminScope:
    id: str            # "admin.tools"
    label: str         # "Tools"
    group: str         # "AI Configuration" — mirrors the SPA nav groups
    description: str
    delegable: bool    # False ⇒ system_admin only

ADMIN_SCOPES: tuple[AdminScope, ...] = (...)
NON_DELEGABLE_SCOPES = frozenset(s.id for s in ADMIN_SCOPES if not s.delegable)
```

Initial set — deliberately **1:1 with the admin router packages**, so the scope
boundary is a directory boundary and stays auditable:

| Scope | Router package | Delegable |
|---|---|---|
| `admin.costs` | `admin/costs/` | yes |
| `admin.quota` | `admin/quota/` | yes |
| `admin.models` | `admin/routes.py` (managed models + provider catalogs) | yes |
| `admin.tools` | `admin/tools/` | yes |
| `admin.skills` | `admin/skills/` | yes |
| `admin.connectors` | `admin/oauth/` | yes |
| `admin.file_sources` | `admin/file_sources/` | yes |
| `admin.export_targets` | `admin/export_targets/` | yes |
| `admin.marketplace` | `admin/agents/` + `admin/roles/agent_pins.py` | yes |
| `admin.system_prompts` | `admin/system_prompts/` | yes |
| `admin.user_menu_links` | `admin/user_menu_links/` | yes |
| `admin.fine_tuning` | `admin/fine_tuning/` | yes |
| `admin.users` | `admin/users/` (read-only surface today) | yes |
| `admin.roles` | `admin/roles/` | **no — I1** |
| `admin.auth_providers` | `admin/auth_providers/` | **no — I1** |

Three notes:

- **Three routers are conditionally mounted** — `skills` (`if skills_enabled()`,
  `routes.py:892`), `fine_tuning` (`FINE_TUNING_ENABLED`, `routes.py:936`), and
  `agents`, which is always mounted but 404s through `require_marketplace_admin`
  when `AGENT_MARKETPLACE_ENABLED` is off. Their scopes remain in the registry
  regardless; a scope for an unmounted router is simply inert. §6.3's coverage
  test has to account for this or it fails in any environment with a flag off.
- **`admin.marketplace` spans two packages.** `roles/agent_pins.py` mounts under
  the `/roles` prefix but is marketplace functionality guarded by
  `require_marketplace_admin` (`admin/agents/routes.py:91`). It maps to
  `admin.marketplace`, *not* `admin.roles` — the one place the directory-boundary
  heuristic needs an explicit exception, so it gets an explicit test.
- **No `:read`/`:write` split in v1** (decided 2026-07-27). `admin.costs` and
  `admin.users` are the only areas where a read-only tier is obviously useful,
  and both are read-only surfaces already. Adding a verb axis doubles the
  registry and the UI for one speculative case; the registry is designed so
  `admin.costs:read` can be added later without reshaping anything.

---

## 6. Backend enforcement

### 6.1 The dependency

```python
# apis/shared/auth/rbac.py
def require_admin_scope(scope: str) -> Callable:
    """Authorize a delegated admin surface. `system_admin` satisfies every scope."""
    async def checker(user: User = Depends(get_current_user_from_session)) -> User:
        from apis.shared.rbac.service import get_app_role_service
        try:
            permissions = await get_app_role_service().resolve_user_permissions(user)
            if "system_admin" in permissions.app_roles:
                return user
            if scope in permissions.admin_scopes:
                return user
        except Exception:
            logger.exception("Failed to resolve admin scope for %s, denying", user.name)
        raise HTTPException(status_code=403, detail="Access denied.")
    return checker
```

Note it takes a **single** scope, not varargs. `require_app_roles(*roles)` is OR
logic; an OR over admin scopes has no legitimate use here and would make the
architecture test in §6.3 much weaker.

### 6.2 Migrating 112 call sites

Do **not** hand-edit 112 `Depends(...)` expressions, and do not use router-level
`dependencies=[...]` (the handlers need the injected `User`, so the per-handler
dependency has to stay). Instead each admin package declares its scope once at
module level and the handlers reference the alias:

```python
# apis/app_api/admin/tools/routes.py
_require = require_admin_scope("admin.tools")

@router.get("")
async def list_tools(admin: User = Depends(_require)):
```

One mechanical substitution per file, 15 files. `admin/roles/routes.py` and
`admin/auth_providers/routes.py` keep bare `require_admin` (I1).

The migration is fully contained: `require_admin` is *invoked* nowhere outside
`app_api/admin/`. It appears in `agent_designer/routes.py`,
`agent_designer/services/listing_service.py`, and `shared/assistants/listing.py`
only in comments describing the reviewer role, so those files need no change.

### 6.3 The test that stops this rotting

An architecture test (alongside `tests/architecture/test_import_boundaries.py`)
that walks every route on the `/admin` router and asserts:

1. every route's auth dependency is either `require_admin` (and its module is on
   the non-delegable whitelist) or a `require_admin_scope` with an id in
   `ADMIN_SCOPES`;
2. no route resolves to a scope in `NON_DELEGABLE_SCOPES`;
3. every scope in `ADMIN_SCOPES` is claimed by at least one *module* — asserted
   against the source tree, not the mounted router, so it still holds when
   `SKILLS_ENABLED` / `FINE_TUNING_ENABLED` are off. This catches a scope that
   outlives its router and becomes a grantable no-op;
4. `agent_pins.py` maps to `admin.marketplace`.

Without (1) the failure mode is a new admin route added with no scope, silently
open to every delegated admin. That is the regression this feature must not ship.

---

## 7. Frontend

`UserPermissions` already carries `appRoles`, `tools`, `models`, `quotaTier` from
`GET /users/me/permissions` — and the SPA **discards all but `appRoles`**
(`user.service.ts:79-90`). Add `adminScopes` to the payload and actually read it.

```ts
// auth/user.service.ts
readonly adminScopes = signal<string[]>([]);
readonly isAdmin = computed(() => this.appRoles().includes('system_admin'));   // unchanged
readonly canAccessAdmin = computed(() => this.isAdmin() || this.adminScopes().length > 0);
hasAdminScope(scope: string) { return this.isAdmin() || this.adminScopes().includes(scope); }
```

`isAdmin` keeps its exact current meaning — it still gates things that are
genuinely superuser-only. `canAccessAdmin` is the new "may enter `/admin`" signal
and drives the parent `adminGuard` and the "Admin Dashboard" item in
`user-dropdown.component.ts:119`.

Three pieces of real work:

1. **Per-route guards.** Today zero child routes are guarded. Each entry in
   `admin.routes.ts` gains `canActivate: [adminScopeGuard], data: { scope: 'admin.tools' }`.
   Including the nested `quotaRoutes` and the `fine-tuning` / `marketplace`
   children.
2. **Nav filtering.** `navGroups` in `admin.layout.ts:238` is a pure pass-through
   `computed`, but it is *shaped* for exactly this — a previous commit filtered
   the Skills item out of it and was reverted. Add `scope` to `NavItem`, filter
   items by `hasAdminScope`, drop groups left empty. Mobile `<select>` and desktop
   `<nav>` both iterate `navGroups()`, so one filter fixes both.
3. **The landing redirect — the trap.** `/admin` currently redirects `'' → costs`
   (`admin.routes.ts:5-9`). A skills-only admin would land on Cost Analytics and
   bounce. Replace the static redirect with a resolver that sends the user to the
   first nav item they can actually reach.

Also worth doing: the marketplace badge counts fire on **every** admin page
(`ngOnInit` → `refreshQueueCounts()`, `admin.layout.ts:254`). For an admin without
`admin.marketplace` that is a guaranteed 403 on every navigation. Gate the call
on the scope.

---

## 8. Resolved decisions (2026-07-27)

- **Audit logging is in scope** — PR-5, part of this epic rather than deferred to
  a broader observability effort. There is no audit trail on role or admin
  mutations today; with one superuser that was tolerable, with delegated admins
  "who changed this?" becomes a real question.

  Worth noting the foundation is already half-built: `admin_service` emits
  structured log records on every mutation — `extra={"event": "app_role_created",
  "role_id": …, "admin_user_id": …, "admin_email": …}` at `admin_service.py:99`,
  `:174`, `:218`. That is a log line, not a queryable trail: no retention
  guarantee, no before/after values, and nothing an admin can read from the
  console. PR-5 turns the existing emission points into durable records rather
  than inventing a new instrumentation pass.

- **The ~5-minute scope-revocation lag is accepted** (§4.2). It is the same
  window that already applies to removing someone from `system_admin`. Recorded
  as a known property; no distributed-cache work in this epic.

- **No `:read`/`:write` split in v1** — see §5.

- **Seeding: no example delegated role.** An unused role that grants admin power
  is a liability, and the feature is inert and safe with zero scoped roles.

---

## 9. Phasing

| PR | Content |
|---|---|
| **PR-1** | Data + registry: `granted_admin_scopes` on `AppRole`, `admin_scopes` through `EffectivePermissions`/`UserEffectivePermissions`/`_merge_permissions`, `admin_scopes.py` registry, non-delegable + unknown-scope validation in `role_constraints.py` wired into `admin_service`, `grantedAdminScopes` on the `AppRoleCreate`/`AppRoleUpdate` bodies so the axis round-trips end to end. Unit tests. **Inert** — the value is stored and resolved, but no authorization path reads it. |
| **PR-2** | Enforcement: `require_admin_scope`, the 15-file alias migration, the I3 write-through guard on `set_roles_for_*`, the §6.3 architecture test. Behavior for `system_admin` unchanged. Also fix the two stale auth docs while the context is loaded — `apis/app_api/admin/README.md` and `apis/shared/auth/RBAC_QUICK_REFERENCE.md` both document helpers that do not exist (`require_roles`, `require_all_roles`, `has_any_role`, `require_faculty`, …) and both describe `require_admin` as "Admin or SuperAdmin". Anyone implementing delegated admin will read them first. |
| **PR-3** | API surface: `adminScopes` on `/users/me/permissions`, `GET /admin/roles/admin-scopes` registry endpoint. Watch the precedent bug here: `UserPermissionsResponse` (`app_api/users/routes.py:37-43`) still omits `skills` even though `UserEffectivePermissions` has carried it for months — a field added to the model and forgotten in the response shape. Add `skills` while in there. |
| **PR-4** | SPA: `adminScopes` signal + `hasAdminScope`/`canAccessAdmin`, per-route `adminScopeGuard`, nav filtering, landing resolver, scoped badge refresh, admin-scopes control on the role form. |
| **PR-5** | Audit log for admin-surface mutations (decided in scope, §8). Promotes the existing structured-log emission points into durable, queryable records with before/after values. |

**No feature flag.** The repo's convention is default-on-with-kill-switch, but a
flag here would add risk rather than remove it: the mechanism is inert until a
system admin writes `grantedAdminScopes` on some role, and until then every code
path resolves exactly as it does today. The safe default *is* the empty set.

PR-1 and PR-2 must land together in a release — PR-1 alone stores a grant that
nothing enforces, which is worse than not having it.
