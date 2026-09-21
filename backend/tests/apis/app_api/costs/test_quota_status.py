"""API surface for the user-facing quota-status read.

Covers the three states the UI must distinguish (normal / unlimited /
unconfigured) and asserts the status read records NO quota events.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.costs.routes import router as costs_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from agents.main_agent.quota.models import QuotaTier, ResolvedQuota


def _user() -> User:
    return User(
        email="user@example.com",
        user_id="user-1",
        name="Test User",
        roles=["Faculty"],
    )


def _tier(**overrides) -> QuotaTier:
    defaults = dict(
        tier_id="tier-standard",
        tier_name="Standard",
        monthly_cost_limit=10.0,
        period_type="monthly",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        created_by="admin",
    )
    defaults.update(overrides)
    return QuotaTier(**defaults)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(costs_router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return TestClient(app)


def _patch_quota(resolved: ResolvedQuota | None, total_cost: float = 0.0):
    """Patch the lazily-imported quota singletons used by the route."""
    resolver = SimpleNamespace(
        resolve_user_quota=AsyncMock(return_value=resolved)
    )
    aggregator = SimpleNamespace(
        get_user_cost_summary=AsyncMock(
            return_value=SimpleNamespace(total_cost=total_cost)
        )
    )
    return (
        patch("apis.shared.quota.get_quota_resolver", return_value=resolver),
        patch("apis.shared.quota.get_cost_aggregator", return_value=aggregator),
    )


def test_normal_tier_returns_limit_and_percentage(client) -> None:
    resolved = ResolvedQuota(
        user_id="user-1", tier=_tier(monthly_cost_limit=10.0), matched_by="jwt_role:Faculty"
    )
    p_res, p_agg = _patch_quota(resolved, total_cost=2.5)
    with p_res, p_agg:
        resp = client.get("/costs/quota-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["unlimited"] is False
    assert body["monthlyLimit"] == 10.0
    assert body["currentUsage"] == 2.5
    assert body["remaining"] == 7.5
    assert body["usagePercentage"] == 25.0
    assert body["tierName"] == "Standard"
    assert body["periodType"] == "monthly"
    assert body["resetInfo"]


def test_unlimited_tier_has_no_denominator(client) -> None:
    resolved = ResolvedQuota(
        user_id="user-1",
        tier=_tier(tier_name="Unlimited", monthly_cost_limit=999999),
        matched_by="override",
    )
    p_res, p_agg = _patch_quota(resolved)
    with p_res, p_agg:
        resp = client.get("/costs/quota-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["unlimited"] is True
    assert body["monthlyLimit"] is None
    assert body["usagePercentage"] == 0.0


def test_no_tier_returns_unconfigured(client) -> None:
    p_res, p_agg = _patch_quota(None)
    with p_res, p_agg:
        resp = client.get("/costs/quota-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["monthlyLimit"] is None


def test_status_read_records_no_quota_events(client) -> None:
    """A status read must be side-effect-free: no event recorder is touched."""
    resolved = ResolvedQuota(
        user_id="user-1", tier=_tier(), matched_by="direct_user"
    )
    p_res, p_agg = _patch_quota(resolved, total_cost=9.9)
    with p_res, p_agg, patch(
        "apis.shared.quota.get_event_recorder"
    ) as recorder:
        resp = client.get("/costs/quota-status")

    assert resp.status_code == 200
    recorder.assert_not_called()
