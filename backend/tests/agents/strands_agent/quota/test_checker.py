"""Unit tests for QuotaChecker."""

import pytest
from unittest.mock import AsyncMock, Mock
from datetime import datetime
from agents.strands_agent.quota.checker import QuotaChecker
from agents.strands_agent.quota.resolver import QuotaResolver
from agents.strands_agent.quota.event_recorder import QuotaEventRecorder
from agents.strands_agent.quota.models import (
    QuotaTier,
    QuotaAssignment,
    QuotaAssignmentType,
    ResolvedQuota,
    QuotaCheckResult
)
from apis.shared.auth.models import User
from apis.app_api.costs.aggregator import CostAggregator
from apis.app_api.costs.models import UserCostSummary


@pytest.fixture
def mock_resolver():
    """Create a mock quota resolver"""
    resolver = Mock(spec=QuotaResolver)
    resolver.resolve_user_quota = AsyncMock()
    return resolver


@pytest.fixture
def mock_cost_aggregator():
    """Create a mock cost aggregator"""
    aggregator = Mock(spec=CostAggregator)
    aggregator.get_user_cost_summary = AsyncMock()
    return aggregator


@pytest.fixture
def mock_event_recorder():
    """Create a mock event recorder"""
    recorder = Mock(spec=QuotaEventRecorder)
    recorder.record_block = AsyncMock()
    return recorder


@pytest.fixture
def checker(mock_resolver, mock_cost_aggregator, mock_event_recorder):
    """Create a QuotaChecker with mocks"""
    return QuotaChecker(
        resolver=mock_resolver,
        cost_aggregator=mock_cost_aggregator,
        event_recorder=mock_event_recorder
    )


@pytest.fixture
def sample_user():
    """Create a sample user"""
    return User(
        user_id="test123",
        email="test@example.com",
        name="Test User",
        roles=["Student"]
    )


@pytest.fixture
def sample_tier():
    """Create a sample quota tier"""
    return QuotaTier(
        tier_id="premium",
        tier_name="Premium Tier",
        monthly_cost_limit=500.0,
        daily_cost_limit=20.0,
        period_type="monthly",
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )


@pytest.fixture
def sample_assignment():
    """Create a sample assignment"""
    return QuotaAssignment(
        assignment_id="assign1",
        tier_id="premium",
        assignment_type=QuotaAssignmentType.DIRECT_USER,
        user_id="test123",
        priority=300,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )


@pytest.mark.asyncio
async def test_check_quota_no_quota_configured(
    checker, mock_resolver, sample_user
):
    """Test quota check when no quota is configured"""
    # No quota resolved
    mock_resolver.resolve_user_quota.return_value = None

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions
    assert result.allowed is True
    assert result.message == "No quota configured"
    assert result.tier is None
    assert result.current_usage == 0.0


@pytest.mark.asyncio
async def test_check_quota_within_limits(
    checker, mock_resolver, mock_cost_aggregator, sample_user, sample_tier, sample_assignment
):
    """Test quota check when user is within limits"""
    # Setup resolved quota
    resolved = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Setup cost summary (within limit)
    cost_summary = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=250.0,  # 250 / 500 = 50%
        models=[],
        totalRequests=100,
        totalInputTokens=50000,
        totalOutputTokens=25000,
        totalCacheSavings=10.0
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = cost_summary

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions
    assert result.allowed is True
    assert result.message == "Within quota"
    assert result.tier.tier_id == "premium"
    assert result.current_usage == 250.0
    assert result.quota_limit == 500.0
    assert result.percentage_used == 50.0
    assert result.remaining == 250.0


@pytest.mark.asyncio
async def test_check_quota_exceeded(
    checker, mock_resolver, mock_cost_aggregator, mock_event_recorder,
    sample_user, sample_tier, sample_assignment
):
    """Test quota check when user exceeds limit"""
    # Setup resolved quota
    resolved = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Setup cost summary (exceeded limit)
    cost_summary = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=550.0,  # 550 / 500 = 110%
        models=[],
        totalRequests=200,
        totalInputTokens=100000,
        totalOutputTokens=50000,
        totalCacheSavings=20.0
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = cost_summary

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions
    assert result.allowed is False
    assert "Quota exceeded" in result.message
    assert result.tier.tier_id == "premium"
    assert result.current_usage == 550.0
    assert result.quota_limit == 500.0
    assert abs(float(result.percentage_used) - 110.0) < 0.01  # Allow for floating point precision
    assert result.remaining == 0.0

    # Verify block event was recorded
    mock_event_recorder.record_block.assert_called_once()
    call_args = mock_event_recorder.record_block.call_args
    assert call_args.kwargs['user'].user_id == "test123"
    assert call_args.kwargs['tier'].tier_id == "premium"
    assert call_args.kwargs['current_usage'] == 550.0
    assert call_args.kwargs['limit'] == 500.0


@pytest.mark.asyncio
async def test_check_quota_unlimited_tier(
    checker, mock_resolver, sample_user, sample_assignment
):
    """Test quota check with unlimited tier"""
    # Setup unlimited tier
    unlimited_tier = QuotaTier(
        tier_id="unlimited",
        tier_name="Unlimited Tier",
        monthly_cost_limit=999999.0,  # Very high limit
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    resolved = ResolvedQuota(
        user_id="test123",
        tier=unlimited_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions
    assert result.allowed is True
    assert result.message == "Unlimited quota"
    assert result.tier.tier_id == "unlimited"
    assert result.percentage_used == 0.0


@pytest.mark.asyncio
async def test_check_quota_daily_period(
    checker, mock_resolver, mock_cost_aggregator, sample_user, sample_assignment
):
    """Test quota check with daily period type"""
    # Setup daily tier
    daily_tier = QuotaTier(
        tier_id="daily",
        tier_name="Daily Tier",
        monthly_cost_limit=500.0,
        daily_cost_limit=20.0,
        period_type="daily",
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    resolved = ResolvedQuota(
        user_id="test123",
        tier=daily_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Setup cost summary
    cost_summary = UserCostSummary(
        userId="test123",
        periodStart="2025-01-17T00:00:00Z",
        periodEnd="2025-01-17T23:59:59Z",
        totalCost=15.0,  # 15 / 20 = 75%
        models=[],
        totalRequests=50,
        totalInputTokens=25000,
        totalOutputTokens=12500,
        totalCacheSavings=5.0
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = cost_summary

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions
    assert result.allowed is True
    assert result.quota_limit == 20.0  # Uses daily limit
    assert result.percentage_used == 75.0


@pytest.mark.asyncio
async def test_check_quota_cost_aggregator_error(
    checker, mock_resolver, mock_cost_aggregator, sample_user, sample_tier, sample_assignment
):
    """Test quota check handles cost aggregator errors gracefully"""
    # Setup resolved quota
    resolved = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Simulate cost aggregator error
    mock_cost_aggregator.get_user_cost_summary.side_effect = Exception("DynamoDB error")

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions - should allow request on error
    assert result.allowed is True
    assert "Error checking quota" in result.message
    assert result.current_usage == 0.0


@pytest.mark.asyncio
async def test_check_quota_exactly_at_limit(
    checker, mock_resolver, mock_cost_aggregator, mock_event_recorder,
    sample_user, sample_tier, sample_assignment
):
    """Test quota check when usage exactly equals limit"""
    # Setup resolved quota
    resolved = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_resolver.resolve_user_quota.return_value = resolved

    # Setup cost summary (exactly at limit)
    cost_summary = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=500.0,  # Exactly 500
        models=[],
        totalRequests=200,
        totalInputTokens=100000,
        totalOutputTokens=50000,
        totalCacheSavings=20.0
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = cost_summary

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions - at limit = blocked
    assert result.allowed is False
    assert result.percentage_used == 100.0

    # Verify block event was recorded
    mock_event_recorder.record_block.assert_called_once()
