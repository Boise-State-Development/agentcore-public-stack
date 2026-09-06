"""Every admin route is governed by exactly one known admin scope.

The regression this exists to prevent: someone adds a route under ``/admin``
and forgets the scope, leaving it reachable by *every* delegated admin. That
failure is silent — the route works, the tests pass, and the hole is only
visible by reading the dependency.

So rather than trusting review, this walks the mounted admin router and asserts
every route resolves to either a registry scope or one of the two explicitly
non-delegable modules.

See ``docs/specs/granular-admin-permissions.md`` §6.3.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from apis.shared.rbac.admin_scopes import (
    ALL_SCOPE_IDS,
    NON_DELEGABLE_SCOPES,
)

ADMIN_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "apis" / "app_api" / "admin"
)

# The only modules permitted to guard routes with bare `require_admin`.
# Both are non-delegable by design; see the module docstrings on each.
NON_DELEGABLE_MODULES = {
    "roles/routes.py",
    "auth_providers/routes.py",
    "audit/routes.py",
}

# Scope claimed by each router module. `roles/agent_pins.py` is the one place
# the directory-boundary heuristic doesn't hold: it mounts under the /roles
# prefix but is marketplace functionality, guarded by require_marketplace_admin.
EXPECTED_MODULE_SCOPES = {
    "routes.py": "admin.models",
    "quota/routes.py": "admin.quota",
    "costs/routes.py": "admin.costs",
    "users/routes.py": "admin.users",
    "tools/routes.py": "admin.tools",
    "skills/routes.py": "admin.skills",
    "agents/routes.py": "admin.marketplace",
    "oauth/routes.py": "admin.connectors",
    "file_sources/routes.py": "admin.file_sources",
    "export_targets/routes.py": "admin.export_targets",
    "user_menu_links/routes.py": "admin.user_menu_links",
    "announcements/routes.py": "admin.announcements",
    "system_prompts/routes.py": "admin.system_prompts",
    "fine_tuning/routes.py": "admin.fine_tuning",
}

# Each admin package names its dependency after its area (mirroring the
# pre-existing `require_marketplace_admin`), so tests and readers have a
# stable public handle instead of a private `_require`.
_SCOPE_DECL = re.compile(r'^require_\w+\s*=\s*require_admin_scope\(\s*"([^"]+)"\s*\)', re.M)


def _admin_route_modules() -> list[pathlib.Path]:
    """Every module under app_api/admin/ that declares an APIRouter."""
    return sorted(
        p
        for p in ADMIN_SRC.rglob("*.py")
        if "__pycache__" not in p.parts
        and "APIRouter(" in p.read_text()
    )


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ADMIN_SRC))


# ---------------------------------------------------------------------------
# Static checks — hold regardless of which feature flags are on, because they
# read the source tree rather than the mounted router. `skills`, `fine_tuning`,
# and the marketplace are conditionally mounted.
# ---------------------------------------------------------------------------


def test_every_router_module_is_accounted_for() -> None:
    """A new admin router package must be classified, not silently ungoverned."""
    found = {_rel(p) for p in _admin_route_modules()}
    known = set(EXPECTED_MODULE_SCOPES) | NON_DELEGABLE_MODULES | {
        "roles/agent_pins.py",  # inherits admin.marketplace via the wrapper
    }

    assert found == known, (
        "Admin router modules changed. Every module must either declare an "
        "admin scope or be explicitly non-delegable.\n"
        f"  unclassified: {sorted(found - known)}\n"
        f"  missing:      {sorted(known - found)}"
    )


@pytest.mark.parametrize(
    "module,expected", sorted(EXPECTED_MODULE_SCOPES.items())
)
def test_module_declares_its_expected_scope(module: str, expected: str) -> None:
    source = (ADMIN_SRC / module).read_text()
    declared = _SCOPE_DECL.findall(source)

    assert declared == [expected], (
        f"{module} should declare exactly one scope ({expected}), found {declared}"
    )


@pytest.mark.parametrize("module", sorted(EXPECTED_MODULE_SCOPES))
def test_scoped_module_does_not_use_bare_require_admin(module: str) -> None:
    """A scoped package must not also hold a full-admin dependency."""
    source = (ADMIN_SRC / module).read_text()
    stripped = source.replace("require_admin_scope", "")

    assert "require_admin" not in stripped, (
        f"{module} is scope-governed but still references bare require_admin"
    )


@pytest.mark.parametrize("module", sorted(NON_DELEGABLE_MODULES))
def test_non_delegable_module_uses_require_admin(module: str) -> None:
    source = (ADMIN_SRC / module).read_text()
    stripped = source.replace("require_admin_scope", "")

    assert "require_admin" in stripped, f"{module} must keep bare require_admin"
    assert not _SCOPE_DECL.findall(source), (
        f"{module} is non-delegable and must not declare an admin scope"
    )


def test_declared_scopes_are_all_known_and_delegable() -> None:
    for module, scope in EXPECTED_MODULE_SCOPES.items():
        assert scope in ALL_SCOPE_IDS, f"{module} declares unknown scope {scope}"
        assert scope not in NON_DELEGABLE_SCOPES, (
            f"{module} declares non-delegable scope {scope} — a route guarded by "
            "a non-delegable scope would be unreachable by anyone but system_admin, "
            "which is what bare require_admin is for"
        )


def test_every_delegable_scope_is_claimed_by_a_module() -> None:
    """Catches a scope that outlives its router and becomes a grantable no-op.

    Asserted against the source tree rather than the mounted app so it still
    holds when SKILLS_ENABLED / FINE_TUNING_ENABLED are off.
    """
    claimed = set(EXPECTED_MODULE_SCOPES.values())
    delegable = ALL_SCOPE_IDS - NON_DELEGABLE_SCOPES

    assert delegable == claimed, (
        "Every delegable scope must govern at least one router module.\n"
        f"  granted but unused: {sorted(delegable - claimed)}\n"
        f"  used but unlisted:  {sorted(claimed - delegable)}"
    )


def test_agent_pins_inherits_the_marketplace_scope() -> None:
    """The one documented exception to the directory-boundary rule."""
    source = (ADMIN_SRC / "roles" / "agent_pins.py").read_text()

    assert "require_marketplace_admin" in source
    assert not _SCOPE_DECL.findall(source), (
        "agent_pins.py should inherit the marketplace scope through "
        "require_marketplace_admin, not declare its own"
    )


# ---------------------------------------------------------------------------
# Runtime check — walk the routes actually mounted in this configuration.
# ---------------------------------------------------------------------------


def test_every_mounted_admin_route_has_a_scope_dependency() -> None:
    """No route under /admin may be reachable without a scope or full-admin check.

    This is the backstop for the static checks: it catches a handler that
    declares no auth dependency at all, which no amount of module-level
    grepping would notice.
    """
    from apis.app_api.admin.routes import router as admin_router

    ungoverned = []

    for route in admin_router.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue

        sources = []
        for dep in dependant.dependencies:
            call = dep.call
            if call is None:
                continue
            try:
                sources.append(inspect.getsource(call))
            except (OSError, TypeError):
                sources.append(getattr(call, "__qualname__", ""))

        blob = "\n".join(sources)
        # `checker` is the closure returned by require_app_roles /
        # require_admin_scope; require_marketplace_admin wraps one of them.
        #
        # Two markers, because the two checkers now resolve permissions differently:
        # `require_app_roles` calls `resolve_user_permissions` inline, while
        # `require_admin_scope` delegates to the shared `has_admin_scope` predicate (which
        # the invocation path's reviewer preview also needs, and which cannot be a FastAPI
        # dependency there). This is a source grep one level deep, so a checker that moves
        # its resolution behind another name must add that name here — the guarantee is
        # unchanged, the string that evidences it is not.
        governed = (
            "resolve_user_permissions" in blob
            or "has_admin_scope" in blob
            or "require_marketplace_admin" in blob
            or "agent_marketplace_enabled" in blob
        )
        if not governed:
            ungoverned.append(f"{sorted(route.methods)} {route.path}")

    assert not ungoverned, (
        "Admin routes with no authorization dependency:\n  "
        + "\n  ".join(sorted(ungoverned))
    )
