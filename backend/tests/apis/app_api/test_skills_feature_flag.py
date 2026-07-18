"""Tests for the SKILLS_ENABLED feature gate.

Skills v2 PR-5 flipped this to **default ON with a kill switch** (the
``SCHEDULED_RUNS_ENABLED`` house style): unset or empty resolves enabled, only
the literal ``"false"`` disables. These tests pin three things:

* the ``skills_enabled()`` helper's parsing + default-ON behavior — including
  the empty-string case, which an unset GitHub Actions variable produces and
  which must NOT read as disabled,
* that the admin skills subrouter mounts with the flag on and unmounts with it
  explicitly off, and
* that the ``skills`` capability — not the flag — is what gates *who* sees the
  user-facing surfaces, and that it 404s (never 403s) so the SPA hides the nav
  entry rather than surfacing an error.

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


class TestSkillsCapabilityGate:
    """PR-5 keeps the surfaces admin-only while the flag itself is on.

    Two independent controls: ``SKILLS_ENABLED`` says the feature exists in this
    environment, the ``skills`` capability says who sees it. GA is one grant of
    ``skills`` to the ``default`` role — no redeploy.
    """

    @pytest.mark.asyncio
    async def test_holder_passes_through(self, monkeypatch):
        from apis.app_api.skills import routes as skills_routes

        user = _user()
        monkeypatch.setattr(
            skills_routes, "user_has_capability", _capability_stub(True)
        )
        assert await skills_routes.require_skills_capability(current=user) is user

    @pytest.mark.asyncio
    async def test_non_holder_gets_404_not_403(self, monkeypatch):
        """404 on purpose: the SPA hides the nav entry by riding this call.

        A 403 would surface an error toast instead of hiding the surface — the
        failure mode that got the scheduled-runs capability gate reverted in
        prod. Pin the status so a well-meaning "403 is more correct" change
        can't silently reintroduce it.
        """
        from fastapi import HTTPException

        from apis.app_api.skills import routes as skills_routes

        monkeypatch.setattr(
            skills_routes, "user_has_capability", _capability_stub(False)
        )
        with pytest.raises(HTTPException) as exc:
            await skills_routes.require_skills_capability(current=_user())
        assert exc.value.status_code == 404

    def test_every_user_facing_route_is_gated(self):
        """No user-facing skills route may depend on the session directly.

        A new route added with the bare session dependency would be reachable
        by the whole org the moment the flag went on — the gate has to be the
        default, not a thing each route remembers.
        """
        from apis.app_api.skills import routes as skills_routes

        ungated = []
        for route in skills_routes.router.routes:
            deps = getattr(route, "dependant", None)
            names = {
                sub.call.__name__
                for sub in getattr(deps, "dependencies", [])
                if getattr(sub, "call", None)
            }
            if "require_skills_capability" not in names:
                ungated.append(getattr(route, "path", "?"))

        assert ungated == [], f"ungated skills routes: {ungated}"


def _user():
    from apis.shared.auth.models import User

    return User(email="u@example.edu", user_id="u-1", name="u", roles=[])


def _capability_stub(result: bool):
    async def _stub(user, capability_id):
        return result

    return _stub
