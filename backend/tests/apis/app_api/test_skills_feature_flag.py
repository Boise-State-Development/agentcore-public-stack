"""Tests for the SKILLS_ENABLED feature gate.

Skills v2 PR-5 flipped this to **default ON with a kill switch** (the
``SCHEDULED_RUNS_ENABLED`` house style): unset or empty resolves enabled, only
the literal ``"false"`` disables. These tests pin three things:

* the ``skills_enabled()`` helper's parsing + default-ON behavior — including
  the empty-string case, which an unset GitHub Actions variable produces and
  which must NOT read as disabled,
* that the admin skills subrouter mounts with the flag on and unmounts with it
  explicitly off, and
* that the flag is now the *only* gate on the user-facing surfaces — every
  route requires a session, and the removed ``skills`` capability gate has not
  crept back.

The admin subrouter mounts conditionally at import time, so — like the
fine-tuning gate in tests/routes/test_admin_lows.py — these reload the module
with the env set, then restore it on teardown.
"""

from __future__ import annotations

import importlib
import os

import pytest

import apis.app_api.admin.routes as admin_routes_module
from apis.shared.feature_flags import skills_enabled


# ---------------------------------------------------------------------------
# skills_enabled() helper
# ---------------------------------------------------------------------------


class TestSkillsEnabledFlag:
    def test_defaults_on_when_unset(self, monkeypatch):
        monkeypatch.delenv("SKILLS_ENABLED", raising=False)
        assert skills_enabled() is True

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("false", False),
            ("False", False),
            ("FALSE", False),
            (" false ", False),
            ("true", True),
            ("0", True),
            ("no", True),
            # An unset GitHub Actions variable forwards as the empty string.
            # It must resolve ENABLED — reading it as "off" is how a kill
            # switch silently dark-ships a live feature.
            ("", True),
            ("   ", True),
        ],
    )
    def test_only_literal_false_disables(self, monkeypatch, value, expected):
        monkeypatch.setenv("SKILLS_ENABLED", value)
        assert skills_enabled() is expected


# ---------------------------------------------------------------------------
# Admin subrouter mount gating
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_router_paths():
    """Reload the admin router under a chosen SKILLS_ENABLED value and return
    its route paths. Restores the module (flag off) on teardown so a reload
    here can't leak mounted skills routes into later tests."""

    def _load(*, enabled: bool) -> set[str]:
        # Under the default-ON flag, "disabled" must be set EXPLICITLY —
        # popping the var now means enabled.
        os.environ["SKILLS_ENABLED"] = "true" if enabled else "false"
        importlib.reload(admin_routes_module)
        return {getattr(route, "path", "") for route in admin_routes_module.router.routes}

    yield _load

    os.environ.pop("SKILLS_ENABLED", None)
    importlib.reload(admin_routes_module)


def test_admin_skills_unmounted_when_disabled(admin_router_paths):
    paths = admin_router_paths(enabled=False)
    assert not any("/skills" in p for p in paths)


def test_admin_skills_mounted_when_enabled(admin_router_paths):
    paths = admin_router_paths(enabled=True)
    assert any("/skills" in p for p in paths)


# ---------------------------------------------------------------------------
# `skills` capability gate on the user-facing surfaces
# ---------------------------------------------------------------------------


class TestSkillsRouteAuth:
    """``SKILLS_ENABLED`` is the only gate on *whether* these routes exist.

    The per-user ``skills`` capability that used to gate them was removed (see
    the "Access model" note in ``apis.app_api.skills.routes``): it could not be
    granted from the admin roles UI, so an admin granting a catalog skill to a
    role had no in-product way to make it visible. Access is now governed by
    ``grantedSkills`` per role, which the roles UI *can* edit.

    What still has to hold is that every route requires a session.
    """

    def test_every_user_facing_route_requires_a_session(self):
        """No skills route may be reachable unauthenticated.

        The capability gate is gone, so ``get_current_user_from_session`` is the
        only thing standing between these routes and an anonymous caller. A new
        route added without it would expose authoring and preferences to the
        internet, so assert the dependency is present on every one rather than
        trusting each route to remember it.
        """
        from apis.app_api.skills import routes as skills_routes

        unauthenticated = []
        for route in skills_routes.router.routes:
            deps = getattr(route, "dependant", None)
            names = _dependency_names(deps)
            if "get_current_user_from_session" not in names:
                unauthenticated.append(getattr(route, "path", "?"))

        assert unauthenticated == [], (
            f"skills routes missing session auth: {unauthenticated}"
        )

    def test_no_capability_gate_remains(self):
        """Pin the removal.

        Re-adding a capability dependency here would silently re-break the
        admin flow (grant a skill to a role → users still can't see it, with no
        UI to fix it). If a future change genuinely needs one, it has to make
        the capability grantable from the roles UI first.
        """
        from apis.app_api.skills import routes as skills_routes

        assert not hasattr(skills_routes, "require_skills_capability")

        gated = [
            getattr(route, "path", "?")
            for route in skills_routes.router.routes
            if any(
                "capability" in name
                for name in _dependency_names(getattr(route, "dependant", None))
            )
        ]
        assert gated == [], f"capability-gated skills routes reintroduced: {gated}"


def _dependency_names(dependant) -> set:
    """Names of a route's sub-dependency callables (empty set if none)."""
    return {
        sub.call.__name__
        for sub in getattr(dependant, "dependencies", [])
        if getattr(sub, "call", None)
    }
