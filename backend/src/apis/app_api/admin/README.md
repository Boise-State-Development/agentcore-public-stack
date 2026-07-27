# Admin API Module

> Rewritten 2026-07-27. The previous version documented RBAC helpers that do not
> exist (`require_roles`, `require_all_roles`, `has_any_role`, `require_faculty`,
> …), five endpoints that are not in this module (`/admin/me`,
> `/admin/sessions/all`, `/admin/stats`, `/admin/conditional-example`,
> `/admin/require-multiple-roles-example`), and an `ENABLE_AUTHENTICATION=false`
> switch that no longer exists (it is `SKIP_AUTH` now). Treat any surviving
> reference to those as stale.

Privileged endpoints, mounted under `/admin`. `routes.py` owns the root router
and includes every sub-router; each sub-package is one admin feature area.

## Authorization model

Admin access is **not** a single bit. Each router package is governed by one
*admin scope* that a system admin can delegate to another role.

```python
# tools/routes.py
from apis.shared.auth import User, require_admin_scope

router = APIRouter(prefix="/tools", tags=["admin-tools"])

require_tools_admin = require_admin_scope("admin.tools")

@router.get("")
async def list_tools(admin: User = Depends(require_tools_admin)):
    ...
```

`system_admin` satisfies every scope implicitly, so a full admin sees no change.
Scope ids come from the closed registry in `apis/shared/rbac/admin_scopes.py`.

**Two packages are non-delegable and keep bare `require_admin`:**

| Package | Why |
|---|---|
| `roles/` | Editing a role is how admin power is handed out — anyone who can PATCH a role can grant themselves anything. |
| `auth_providers/` | Role resolution starts from JWT claims, so controlling IdP attribute mapping controls which AppRoles resolve. Role administration by another route. |

## Feature areas

| Package | Prefix | Scope |
|---|---|---|
| `routes.py` (root) | `/admin` | `admin.models` |
| `quota/` | `/admin/quota` | `admin.quota` |
| `costs/` | `/admin/costs` | `admin.costs` |
| `users/` | `/admin/users` | `admin.users` |
| `tools/` | `/admin/tools` | `admin.tools` |
| `skills/` | `/admin/skills` | `admin.skills` |
| `agents/` | `/admin/agents` | `admin.marketplace` |
| `roles/agent_pins.py` | `/admin/roles/{id}/agent-pins` | `admin.marketplace` ¹ |
| `oauth/` | `/admin/oauth-providers` | `admin.connectors` |
| `file_sources/` | `/admin/file-source-adapters` | `admin.file_sources` |
| `export_targets/` | `/admin/export-target-adapters` | `admin.export_targets` |
| `system_prompts/` | `/admin/system-prompts` | `admin.system_prompts` |
| `user_menu_links/` | `/admin/user-menu-links` | `admin.user_menu_links` |
| `fine_tuning/` | `/admin/fine-tuning` | `admin.fine_tuning` |
| `roles/` | `/admin/roles` | **non-delegable** |
| `auth_providers/` | `/admin/auth-providers` | **non-delegable** |

¹ The one place the "scope = package" rule doesn't hold. `agent_pins.py` mounts
under the `/roles` prefix but is marketplace functionality; it inherits
`admin.marketplace` through `require_marketplace_admin`. Covered by an explicit
test so it can't drift.

**Conditionally mounted:** `skills/` (`SKILLS_ENABLED`), `fine_tuning/`
(`FINE_TUNING_ENABLED`), and `agents/` (always mounted but 404s through
`require_marketplace_admin` when `AGENT_MARKETPLACE_ENABLED` is off).

## Adding an admin endpoint

**To an existing package** — add the route and use the package's existing
`require_<area>_admin` dependency:

```python
@router.post("/{tool_id}/archive")
async def archive_tool(tool_id: str, admin: User = Depends(require_tools_admin)):
    ...
```

**A new feature area** — four steps, and the tests will tell you if you miss one:

1. Add an `AdminScope` to `apis/shared/rbac/admin_scopes.py`.
2. Create the package with `require_<area>_admin = require_admin_scope("admin.<area>")`
   — named after the area, mirroring `require_marketplace_admin`, so tests have
   a stable public handle to override.
3. Include the router in `routes.py`.
4. Register the module → scope mapping in
   `tests/architecture/test_admin_scope_coverage.py`.

That test walks every mounted admin route and fails if one has no authorization
dependency, if a package uses bare `require_admin` without being on the
non-delegable list, or if a registry scope governs nothing. The regression it
exists to catch is a new admin route added with no scope — silently reachable by
every delegated admin.

## RBAC gotcha: write-through

The tool, model, and skill pages each offer a "which roles can use this?" picker
that writes *through* into role records (`set_roles_for_tool` and siblings), all
of which land in `AppRoleAdminService.update_role`. That is intended — granting a
tool is a tool admin's job.

What is guarded: if the target role is protected or carries `grantedAdminScopes`,
the actor must hold `system_admin`. Otherwise a delegated `admin.tools` holder
could add grants to an admin-bearing role from a surface never meant to
administer roles. The check lives in `update_role` rather than the three callers,
so any future resource surface that grows a role picker inherits it. It raises
`RoleMutationForbidden` → 403 (not `ValueError` → 400, which would misreport a
denied escalation as a bad request).

## Errors

| Status | Meaning |
|---|---|
| 401 | No valid session cookie. |
| 403 | Authenticated but lacks the scope, or a blocked role mutation. |
| 404 | Feature area disabled by its kill switch (reads as unmounted). |

## Local development

`SKIP_AUTH=true` returns a fake user whose roles come from `SKIP_AUTH_ROLES`
(default `admin`). Those roles still resolve through the AppRole table, so the
mapped role must reach `system_admin` for admin routes to open. `app_api/main.py`
refuses to boot when `SKIP_AUTH` is combined with deployed-environment
indicators.

## See also

- `apis/shared/auth/RBAC_QUICK_REFERENCE.md` — the dependency surface.
- `docs/specs/granular-admin-permissions.md` — why the scope model is shaped this
  way, including the escalation analysis.
