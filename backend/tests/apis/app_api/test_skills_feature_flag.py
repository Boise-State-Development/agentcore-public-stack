"""Tests for the SKILLS_ENABLED feature gate.

The skills feature (admin catalog, user picker, skills mode) is deferred and
disabled by default. These tests pin two things:

* the ``skills_enabled()`` helper's parsing + default-off behavior, and
* that the admin skills + chat-mode-policy subrouters are unmounted while the
  flag is off and mounted while it is on.

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
    def test_defaults_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("SKILLS_ENABLED", raising=False)
        assert skills_enabled() is False

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("true", True),
            ("True", True),
            ("TRUE", True),
            ("false", False),
            ("0", False),
            ("1", False),
            ("yes", False),
            ("", False),
        ],
    )
    def test_parses_env_value(self, monkeypatch, value, expected):
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
        if enabled:
            os.environ["SKILLS_ENABLED"] = "true"
        else:
            os.environ.pop("SKILLS_ENABLED", None)
        importlib.reload(admin_routes_module)
        return {getattr(route, "path", "") for route in admin_routes_module.router.routes}

    yield _load

    os.environ.pop("SKILLS_ENABLED", None)
    importlib.reload(admin_routes_module)


def test_admin_skills_unmounted_when_disabled(admin_router_paths):
    paths = admin_router_paths(enabled=False)
    assert not any("/skills" in p for p in paths)
    assert not any(p.endswith("/settings/chat") for p in paths)


def test_admin_skills_mounted_when_enabled(admin_router_paths):
    paths = admin_router_paths(enabled=True)
    assert any("/skills" in p for p in paths)
    assert any(p.endswith("/settings/chat") for p in paths)
