"""Platform self-service account tools (read-only pilot).

Covers the two invariants that make the tier safe — identity is captured by
closure and can never be a model argument, and every tool is a read that never
raises — plus the feature-flag gate and the catalog metadata.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.local_tools.account_tools import (
    make_get_my_quota_tool,
    make_get_my_settings_tool,
    make_whoami_tool,
)
from apis.shared.auth.models import User

QUOTA_MODULE = "apis.shared.quota"
SETTINGS_MODULE = "apis.shared.user_settings.repository"


async def _call(tool, *args, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(*args, **kwargs)


def _user(user_id="u1", roles=None):
    return User(
        email=f"{user_id}@boisestate.edu",
        user_id=user_id,
        name="Ada Lovelace",
        roles=roles if roles is not None else ["student"],
    )


def _tier(monthly=20.0, period_type="monthly", daily=None, name="Standard"):
    return SimpleNamespace(
        tier_name=name,
        monthly_cost_limit=monthly,
        period_type=period_type,
        daily_cost_limit=daily,
    )


def _patch_quota(monkeypatch, *, resolved, usage=0.0):
    resolver = SimpleNamespace(
        resolve_user_quota=AsyncMock(return_value=resolved)
    )
    aggregator = SimpleNamespace(
        get_user_cost_summary=AsyncMock(
            return_value=SimpleNamespace(total_cost=usage)
        )
    )
    monkeypatch.setattr(f"{QUOTA_MODULE}.get_quota_resolver", lambda: resolver)
    monkeypatch.setattr(f"{QUOTA_MODULE}.get_cost_aggregator", lambda: aggregator)
    return resolver, aggregator


# ---------------------------------------------------------------------------
# Invariant 1: identity is captured by closure, never a model argument
# ---------------------------------------------------------------------------


class TestIdentityBinding:
    def test_no_tool_accepts_a_user_or_id_argument(self):
        """Structural proof the model cannot redirect a tool at another user:
        the callable takes no parameters at all."""
        for factory in (
            make_whoami_tool,
            make_get_my_quota_tool,
            make_get_my_settings_tool,
        ):
            tool = factory(_user())
            fn = getattr(tool, "__wrapped__", None) or tool
            params = list(inspect.signature(fn).parameters)
            assert params == [], f"{tool.tool_name} must take no arguments, got {params}"

    def test_tool_names_are_stable(self):
        assert make_whoami_tool(_user()).tool_name == "whoami"
        assert make_get_my_quota_tool(_user()).tool_name == "get_my_quota"
        assert make_get_my_settings_tool(_user()).tool_name == "get_my_settings"

    @pytest.mark.asyncio
    async def test_two_tools_bound_to_different_users_do_not_cross(self, monkeypatch):
        _patch_quota(monkeypatch, resolved=None)  # no tier lookups matter here
        a = await _call(make_whoami_tool(_user("alice", ["student"])))
        b = await _call(make_whoami_tool(_user("bob", ["staff"])))
        assert a["email"] == "alice@boisestate.edu" and a["roles"] == ["student"]
        assert b["email"] == "bob@boisestate.edu" and b["roles"] == ["staff"]


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoami:
    @pytest.mark.asyncio
    async def test_returns_identity_and_tier(self, monkeypatch):
        _patch_quota(monkeypatch, resolved=SimpleNamespace(tier=_tier(name="Gold")))
        out = await _call(make_whoami_tool(_user(roles=["student", "beta"])))
        assert out["name"] == "Ada Lovelace"
        assert out["email"] == "u1@boisestate.edu"
        assert out["roles"] == ["student", "beta"]
        assert out["quota_tier"] == "Gold"

    @pytest.mark.asyncio
    async def test_tier_lookup_failure_still_returns_identity(self, monkeypatch):
        resolver = SimpleNamespace(
            resolve_user_quota=AsyncMock(side_effect=RuntimeError("ddb down"))
        )
        monkeypatch.setattr(f"{QUOTA_MODULE}.get_quota_resolver", lambda: resolver)
        out = await _call(make_whoami_tool(_user()))
        assert out["name"] == "Ada Lovelace"
        assert "quota_tier" not in out  # degraded, not raised


# ---------------------------------------------------------------------------
# get_my_quota (read-only; must NOT record quota events)
# ---------------------------------------------------------------------------


class TestGetMyQuota:
    @pytest.mark.asyncio
    async def test_computes_snapshot_from_resolver_and_aggregator(self, monkeypatch):
        _patch_quota(
            monkeypatch,
            resolved=SimpleNamespace(tier=_tier(monthly=20.0, name="Standard")),
            usage=5.0,
        )
        out = await _call(make_get_my_quota_tool(_user()))
        assert out["configured"] is True and out["unlimited"] is False
        assert out["current_usage"] == 5.0
        assert out["quota_limit"] == 20.0
        assert out["remaining"] == 15.0
        assert out["percentage_used"] == 25.0
        assert out["tier"] == "Standard"

    @pytest.mark.asyncio
    async def test_does_not_call_check_quota(self, monkeypatch):
        """A read must not fire the enforcement path (which records warning /
        block events). Guard: QuotaChecker.check_quota is never invoked."""
        import apis.shared.quota as quota_mod

        boom = AsyncMock(side_effect=AssertionError("check_quota must not run"))
        checker = SimpleNamespace(check_quota=boom)
        monkeypatch.setattr(quota_mod, "get_quota_checker", lambda: checker, raising=False)
        _patch_quota(
            monkeypatch,
            resolved=SimpleNamespace(tier=_tier(monthly=20.0)),
            usage=1.0,
        )
        out = await _call(make_get_my_quota_tool(_user()))
        assert out["current_usage"] == 1.0
        boom.assert_not_called()

    @pytest.mark.asyncio
    async def test_unlimited_tier(self, monkeypatch):
        _patch_quota(
            monkeypatch,
            resolved=SimpleNamespace(tier=_tier(monthly=float("inf"), name="Unlimited")),
        )
        out = await _call(make_get_my_quota_tool(_user()))
        assert out["unlimited"] is True and out["configured"] is True

    @pytest.mark.asyncio
    async def test_daily_tier_uses_daily_limit(self, monkeypatch):
        _patch_quota(
            monkeypatch,
            resolved=SimpleNamespace(
                tier=_tier(monthly=100.0, period_type="daily", daily=4.0)
            ),
            usage=1.0,
        )
        out = await _call(make_get_my_quota_tool(_user()))
        assert out["quota_limit"] == 4.0 and out["period_type"] == "daily"
        assert out["remaining"] == 3.0

    @pytest.mark.asyncio
    async def test_no_tier_configured(self, monkeypatch):
        _patch_quota(monkeypatch, resolved=None)
        out = await _call(make_get_my_quota_tool(_user()))
        assert out["configured"] is False and "administrator" in out["message"]

    @pytest.mark.asyncio
    async def test_backend_failure_returns_error_never_raises(self, monkeypatch):
        resolver = SimpleNamespace(
            resolve_user_quota=AsyncMock(side_effect=RuntimeError("boom"))
        )
        monkeypatch.setattr(f"{QUOTA_MODULE}.get_quota_resolver", lambda: resolver)
        out = await _call(make_get_my_quota_tool(_user()))
        assert "error" in out


# ---------------------------------------------------------------------------
# get_my_settings
# ---------------------------------------------------------------------------


class TestGetMySettings:
    @pytest.mark.asyncio
    async def test_returns_default_model(self, monkeypatch):
        repo = SimpleNamespace(
            get_settings=AsyncMock(return_value={"defaultModelId": "anthropic.claude"})
        )
        monkeypatch.setattr(f"{SETTINGS_MODULE}.get_user_settings_repository", lambda: repo)
        out = await _call(make_get_my_settings_tool(_user("u9")))
        repo.get_settings.assert_awaited_once_with("u9")
        assert out["default_model_id"] == "anthropic.claude"

    @pytest.mark.asyncio
    async def test_null_default_model_reads_as_platform_default(self, monkeypatch):
        repo = SimpleNamespace(
            get_settings=AsyncMock(return_value={"defaultModelId": None})
        )
        monkeypatch.setattr(f"{SETTINGS_MODULE}.get_user_settings_repository", lambda: repo)
        out = await _call(make_get_my_settings_tool(_user()))
        assert out["default_model_id"] is None
        assert "platform default" in out["message"]

    @pytest.mark.asyncio
    async def test_backend_failure_returns_error_never_raises(self, monkeypatch):
        repo = SimpleNamespace(get_settings=AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(f"{SETTINGS_MODULE}.get_user_settings_repository", lambda: repo)
        out = await _call(make_get_my_settings_tool(_user()))
        assert "error" in out


# ---------------------------------------------------------------------------
# Feature-flag gate + catalog metadata
# ---------------------------------------------------------------------------


class TestBuilderGate:
    ALL = ["whoami", "get_my_quota", "get_my_settings"]

    def test_disabled_by_default_even_with_ids(self, monkeypatch):
        monkeypatch.delenv("PLATFORM_SELF_SERVICE_ENABLED", raising=False)
        from apis.inference_api.chat.routes import _build_account_tools

        assert _build_account_tools(self.ALL, _user()) == []

    def test_enabled_but_empty_effective_set_builds_nothing(self, monkeypatch):
        """Flag on but the catalog resolved no account tool ids into the turn's
        effective set (rows disabled / ungranted / unseeded) → nothing built."""
        monkeypatch.setenv("PLATFORM_SELF_SERVICE_ENABLED", "true")
        from apis.inference_api.chat.routes import _build_account_tools

        assert _build_account_tools([], _user()) == []
        assert _build_account_tools(None, _user()) == []

    def test_builds_only_ids_present_in_effective_set(self, monkeypatch):
        """The off-switch: a tool absent from the resolved effective set (an
        admin disabled its row, or a role isn't granted it) is not injected,
        even though its factory exists."""
        monkeypatch.setenv("PLATFORM_SELF_SERVICE_ENABLED", "true")
        from apis.inference_api.chat.routes import _build_account_tools

        tools = _build_account_tools(
            ["whoami", "get_my_settings", "some_unrelated_tool"], _user()
        )
        assert {t.tool_name for t in tools} == {"whoami", "get_my_settings"}

    def test_all_three_when_all_resolved(self, monkeypatch):
        monkeypatch.setenv("PLATFORM_SELF_SERVICE_ENABLED", "true")
        from apis.inference_api.chat.routes import _build_account_tools

        tools = _build_account_tools(self.ALL, _user())
        assert {t.tool_name for t in tools} == set(self.ALL)


class TestCatalogMetadata:
    def test_account_tools_are_system_with_correct_visibility(self):
        from agents.main_agent.tools.tool_catalog import TOOL_CATALOG

        whoami = TOOL_CATALOG["whoami"]
        assert whoami.system is True and whoami.hidden is True  # pure plumbing

        for tid in ("get_my_quota", "get_my_settings"):
            meta = TOOL_CATALOG[tid]
            assert meta.system is True and meta.hidden is False  # visible-but-locked
            assert meta.to_dict()["system"] is True
            assert meta.to_dict()["hidden"] is False
