"""Unit tests for QuotaChecker."""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime
from agents.main_agent.quota.checker import QuotaChecker
from agents.main_agent.quota.resolver import QuotaResolver
from agents.main_agent.quota.event_recorder import QuotaEventRecorder
from agents.main_agent.quota.models import (
    QuotaTier,
    QuotaAssignment,
    QuotaAssignmentType,
    ResolvedQuota,
    QuotaCheckResult
)
from apis.shared.auth.models import User
from apis.shared.costs.aggregator import CostAggregator
from apis.shared.costs.models import UserCostSummary


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
    """Test quota check when no quota is configured (fail-closed)"""
    # No quota resolved
    mock_resolver.resolve_user_quota.return_value = None

    # Check quota
    result = await checker.check_quota(sample_user)

    # Assertions - should block when no quota configured
    assert result.allowed is False
    assert result.message == "No quota tier configured. Please contact your administrator."
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

    # Setup cost summary (within limit, below the lowest warning rung)
    cost_summary = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=200.0,  # 200 / 500 = 40%
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
    assert result.warning_level == "none"
    assert result.tier.tier_id == "premium"
    assert result.current_usage == 200.0
    assert result.quota_limit == 500.0
    assert result.percentage_used == 40.0
    assert result.remaining == 300.0


@pytest.mark.asyncio
async def test_check_quota_warns_at_fifty_percent(
    checker, mock_resolver, mock_cost_aggregator, mock_event_recorder,
    sample_user, sample_tier, sample_assignment
):
    """Half a month's budget now warns — the runway rung added by #833 PR-5.

    Before the ladder, a user at 50% heard nothing until 80%, which for a
    pathological session is the same day as the block.
    """
    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = UserCostSummary(
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

    result = await checker.check_quota(sample_user)

    assert result.allowed is True
    assert result.warning_level == "50%"
    assert result.message == "Warning: 50% quota used ($250.00 / $500.00)"
    mock_event_recorder.record_warning_if_needed.assert_awaited_once()
    assert mock_event_recorder.record_warning_if_needed.await_args.kwargs["threshold"] == "50%"


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


@pytest.mark.asyncio
async def test_session_notice_when_one_conversation_is_a_quarter_of_the_month(
    checker, mock_resolver, mock_cost_aggregator, mock_event_recorder,
    sample_user, sample_tier, sample_assignment
):
    """One thread over the tier's share populates the notice fields.

    The user here sits at a placid 12% of their monthly quota — the
    per-user ladder says nothing — while a single conversation has spent
    26% of the limit. That gap is the incident (#833 D5) in miniature.
    """
    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=60.0,  # 60 / 500 = 12%
        models=[],
        totalRequests=10,
        totalInputTokens=1000,
        totalOutputTokens=500,
        totalCacheSavings=0.0
    )

    session_metadata = Mock()
    session_metadata.total_cost = 130.0  # 130 / 500 = 26%, over the 25% share

    with patch(
        "apis.shared.sessions.metadata.get_session_metadata",
        AsyncMock(return_value=session_metadata),
    ):
        result = await checker.check_quota(sample_user, session_id="sess-1")

    assert result.warning_level == "none"
    assert result.session_id == "sess-1"
    assert float(result.session_cost) == 130.0
    assert float(result.session_percentage_of_limit) == pytest.approx(26.0)
    assert float(result.session_notice_threshold) == 25.0
    mock_event_recorder.record_session_notice_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_session_notice_below_the_share(
    checker, mock_resolver, mock_cost_aggregator, mock_event_recorder,
    sample_user, sample_tier, sample_assignment
):
    """A merely ordinary conversation stays silent."""
    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=60.0,
        models=[],
        totalRequests=10,
        totalInputTokens=1000,
        totalOutputTokens=500,
        totalCacheSavings=0.0
    )

    session_metadata = Mock()
    session_metadata.total_cost = 40.0  # 8% of the limit

    with patch(
        "apis.shared.sessions.metadata.get_session_metadata",
        AsyncMock(return_value=session_metadata),
    ):
        result = await checker.check_quota(sample_user, session_id="sess-1")

    assert result.session_cost is None
    mock_event_recorder.record_session_notice_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_notice_survives_a_metadata_read_failure(
    checker, mock_resolver, mock_cost_aggregator, sample_user,
    sample_tier, sample_assignment
):
    """A quota check must never fail because a session row was unreadable."""
    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=60.0,
        models=[],
        totalRequests=10,
        totalInputTokens=1000,
        totalOutputTokens=500,
        totalCacheSavings=0.0
    )

    with patch(
        "apis.shared.sessions.metadata.get_session_metadata",
        AsyncMock(side_effect=Exception("table unavailable")),
    ):
        result = await checker.check_quota(sample_user, session_id="sess-1")

    assert result.allowed is True
    assert result.session_cost is None


@pytest.mark.asyncio
async def test_blocked_turn_skips_the_session_read(
    checker, mock_resolver, mock_cost_aggregator, sample_user,
    sample_tier, sample_assignment
):
    """Nothing to warn about on a turn that is already refused."""
    mock_resolver.resolve_user_quota.return_value = ResolvedQuota(
        user_id="test123",
        tier=sample_tier,
        matched_by="direct_user",
        assignment=sample_assignment
    )
    mock_cost_aggregator.get_user_cost_summary.return_value = UserCostSummary(
        userId="test123",
        periodStart="2025-01-01T00:00:00Z",
        periodEnd="2025-01-31T23:59:59Z",
        totalCost=550.0,  # over the 500 limit
        models=[],
        totalRequests=10,
        totalInputTokens=1000,
        totalOutputTokens=500,
        totalCacheSavings=0.0
    )

    read = AsyncMock()
    with patch("apis.shared.sessions.metadata.get_session_metadata", read):
        result = await checker.check_quota(sample_user, session_id="sess-1")

    assert result.allowed is False
    read.assert_not_awaited()
