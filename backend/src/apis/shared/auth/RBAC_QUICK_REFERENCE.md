# RBAC Quick Reference

> Rewritten 2026-07-27. The previous version documented helpers that do not
> exist (`require_roles`, `require_all_roles`, `has_any_role`, `has_all_roles`,
> `get_current_user`, `require_faculty`, `require_staff`, `require_developer`,
> `require_aws_ai_access`) and described `require_admin` as "Admin or
> SuperAdmin". None of that was accurate. If you find those names in other docs
> or comments, they are stale too.

## What `apis.shared.auth` actually exports

```python
from apis.shared.auth import (
    User,
    get_current_user_from_session,   # authentication
    require_app_roles,               # authorization: AppRole check
    require_admin,                   # authorization: system_admin only
    require_admin_scope,             # authorization: one delegated admin area
)
```

That is the whole surface. There are no per-cohort helpers and no `has_*_role`
predicates — every authorization decision resolves through the AppRole system.

## Authentication

The SPA sends an httpOnly session cookie, not `Authorization: Bearer`. Every
user-facing route under `apis/app_api/` uses:

```python
@router.get("/my-thing")
async def my_thing(user: User = Depends(get_current_user_from_session)):
    ...
```

A Bearer-only dependency on a SPA-facing route causes a 401 → redirect loop.
The only exceptions are the API-key feature (`auth/api_keys/`, `X-API-Key`) and
voice mode (`voice/`, voice-ticket cookie); do not use either as a template.

## Authorization

### Ordinary AppRole check

```python
@router.get("/reports")
async def reports(user: User = Depends(require_app_roles("analyst", "faculty"))):
    ...
```

OR logic across the listed AppRole ids. Fails closed — if permission resolution
raises, access is denied.

### Full admin

```python
@router.post("/roles")
async def create_role(admin: User = Depends(require_admin)):
    ...
```

`require_admin` is exactly `require_app_roles("system_admin")`.

**Reserved for the two non-delegable surfaces** — `admin/roles/` and
`admin/auth_providers/`. Everything else uses a scope (below). An architecture
test enforces this: `tests/architecture/test_admin_scope_coverage.py`.

### Delegated admin area

```python
# apis/app_api/admin/tools/routes.py
_require = require_admin_scope("admin.tools")

@router.get("")
async def list_tools(admin: User = Depends(_require)):
    ...
```

One scope per admin router package, declared once at module level. `system_admin`
satisfies every scope implicitly, so adding a scope never removes access from an
existing admin. Scope ids come from the closed registry in
`apis/shared/rbac/admin_scopes.py` — you cannot invent one, and two
(`admin.roles`, `admin.auth_providers`) can never be granted to a role.

## How a request becomes a permission

1. **IdP → Cognito.** The provider's roles claim is mapped into `custom:roles`
   by the auth-provider attribute mapping.
2. **Login.** The BFF decodes the *ID token* and upserts the Users table. The
   access token carries only a Cognito-internal group name, not the real roles.
3. **Per request.** `get_current_user_from_session` validates the session's
   access token, then `_enrich_user_from_store` overwrites `user.roles` from the
   Users table (the ID-token-derived values). Cached ~5 min, invalidated by the
   roles-version watermark.
4. **JWT role → AppRole.** `AppRoleService.resolve_user_permissions` looks up
   each JWT role via GSI1 (`JWT_ROLE#…`), loads the matching AppRoles, and drops
   disabled ones.
5. **Merge.** Tools, models, skills, and admin scopes union across roles; quota
   tier comes from the highest-`priority` role.

### `default` is a fallback, not a universal role

Step 4 consults `default` **only when the user matched zero AppRoles**. It is
never merged alongside a matched role. Granting something to `default` therefore
reaches only users who match nothing else — *not* everyone. Reaching everyone
means granting to each cohort role.

## The AppRole record is the source of truth

A role grants a tool/model/skill/admin-area by listing it in its own
`grantedTools` / `grantedModels` / `grantedSkills` / `grantedAdminScopes` (or by
granting `*` on the first three, or inheriting from a parent — **admin scopes do
not inherit, and have no `*`**).

`allowedAppRoles` on a resource is a *derived, display-only* projection. It never
grants anything on its own. A "which roles can use this?" picker must write
through to each role's `granted*` list.

## Gotchas

- **The roles UI has no free-text entry.** Its grant controls are built from
  resource catalogs, so an id that is not a catalog entry cannot be granted from
  the UI at all. This is what made the earlier `skills` and `scheduled-runs`
  capability gates inoperable. A new grant axis needs its own registry and its
  own control — that is why `admin_scopes.py` exists.
- **The permission cache is per-process.** `bump_roles_version()` is an
  in-process counter, not distributed, so a revoked grant can survive up to the
  5-minute TTL on ECS tasks that did not serve the mutation.
- **`SKIP_AUTH=true`** (local dev only) returns a fake user whose roles come from
  `SKIP_AUTH_ROLES`. Those roles still have to resolve through the AppRole table
  for `require_admin` to pass. `app_api/main.py` refuses to boot if this is
  combined with deployed-environment indicators.

## Where things live

| Concern | File |
|---|---|
| Dependencies (`require_*`) | `apis/shared/auth/rbac.py` |
| Session authentication | `apis/shared/auth/dependencies.py` |
| Runtime resolution + merge | `apis/shared/rbac/service.py` |
| Role CRUD + effective permissions | `apis/shared/rbac/admin_service.py` |
| Mutation constraints | `apis/shared/rbac/role_constraints.py` |
| Admin scope registry | `apis/shared/rbac/admin_scopes.py` |
| DynamoDB item shapes | `apis/shared/rbac/repository.py` |
