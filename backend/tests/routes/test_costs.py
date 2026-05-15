"""Tests for costs routes.

Endpoints under test:
- GET /costs/summary          → 200 with cost summary (authenticated)
- GET /costs/detailed-report  → 200 with detailed report (authenticated)
- GET /costs/summary          → 401 for unauthenticated request
- GET /costs/detailed-report  → 401 for unauthenticated request

Requirements: 10.1, 10.2
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.costs.routes import router
from apis.shared.costs.models import UserCostSummary
from tests.routes.conftest import mock_auth_user, mock_no_auth

ROUTES_MODULE = "apis.app_api.costs.routes"

SAMPLE_COST_SUMMARY = UserCostSummary(
    userId="user-001",
    periodStart="2025-01-01T00:00:00Z",
    periodEnd="2025-01-31T23:59:59Z",
    totalCost=12.50,
    models=[],
    totalRequests=42,
    totalInputTokens=100000,
    totalOutputTokens=50000,
    totalCacheReadTokens=0,
    totalCacheWriteTokens=0,
    totalCacheSavings=0.0,
)


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the costs router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 10.1: Costs endpoint returns 200 with cost data
# ---------------------------------------------------------------------------


class TestGetCostSummaryAuthenticated:
    """GET /costs/summary returns 200 with cost data for authenticated user."""

    def test_returns_200_with_summary(self, app, make_user):
        """Req 10.1: Authenticated user gets 200 with cost summary."""
        user = make_user()
        mock_auth_user(app, user)

        mock_aggregator = AsyncMock()
        mock_aggregator.get_user_cost_summary.return_value = SAMPLE_COST_SUMMARY

        with patch(f"{ROUTES_MODULE}.CostAggregator", return_value=mock_aggregator):
            client = TestClient(app)
            resp = client.get("/costs/summary")

        assert resp.status_code == 200
        body = resp.json()
        assert body["userId"] == "user-001"
        assert body["totalCost"] == 12.50
        assert body["totalRequests"] == 42

    def test_returns_200_with_period_param(self, app, make_user):
        """Req 10.1: Authenticated user gets 200 with specific period."""
        user = make_user()
        mock_auth_user(app, user)

        mock_aggregator = AsyncMock()
        mock_aggregator.get_user_cost_summary.return_value = SAMPLE_COST_SUMMARY

        with patch(f"{ROUTES_MODULE}.CostAggregator", return_value=mock_aggregator):
            client = TestClient(app)
            resp = client.get("/costs/summary?period=2025-01")

        assert resp.status_code == 200
        mock_aggregator.get_user_cost_summary.assert_called_once_with(
            user_id="user-001", period="2025-01"
        )


class TestGetDetailedReportAuthenticated:
    """GET /costs/detailed-report returns 200 with detailed cost data."""

    def test_returns_200_with_report(self, app, make_user):
        """Req 10.1: Authenticated user gets 200 with detailed report."""
        user = make_user()
        mock_auth_user(app, user)

        mock_aggregator = AsyncMock()
        mock_aggregator.get_detailed_cost_report.return_value = SAMPLE_COST_SUMMARY

        with patch(f"{ROUTES_MODULE}.CostAggregator", return_value=mock_aggregator):
            client = TestClient(app)
            resp = client.get(
                "/costs/detailed-report?start_date=2025-01-01&end_date=2025-01-15"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["userId"] == "user-001"
        assert body["totalCost"] == 12.50


# ---------------------------------------------------------------------------
# Requirement 10.2: Costs endpoint returns 401 for unauthenticated request
# ---------------------------------------------------------------------------


class TestCostsUnauthenticated:
    """All costs endpoints return 401 for unauthenticated requests."""

    def test_summary_returns_401(self, app, unauthenticated_client):
        """Req 10.2: GET /costs/summary returns 401 unauthenticated."""
        client = unauthenticated_client(app)
        assert client.get("/costs/summary").status_code == 401

    def test_detailed_report_returns_401(self, app, unauthenticated_client):
        """Req 10.2: GET /costs/detailed-report returns 401 unauthenticated."""
        client = unauthenticated_client(app)
        resp = client.get(
            "/costs/detailed-report?start_date=2025-01-01&end_date=2025-01-15"
        )
        assert resp.status_code == 401
