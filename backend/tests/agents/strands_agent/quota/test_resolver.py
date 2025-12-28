"""Unit tests for QuotaResolver."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from agents.strands_agent.quota.resolver import QuotaResolver
from agents.strands_agent.quota.repository import QuotaRepository
from agents.strands_agent.quota.models import (
    QuotaTier,
    QuotaAssignment,
    QuotaAssignmentType,
    ResolvedQuota
)
from apis.shared.auth.models import User


@pytest.fixture
def mock_repository():
    """Create a mock quota repository"""
    repo = Mock(spec=QuotaRepository)
    # Make all methods async mocks
    repo.query_user_assignment = AsyncMock()
    repo.query_role_assignments = AsyncMock()
    repo.list_assignments_by_type = AsyncMock()
    repo.get_tier = AsyncMock()
    repo.get_active_override = AsyncMock(return_value=None)  # Default: no override
    return repo


@pytest.fixture
def resolver(mock_repository):
    """Create a QuotaResolver with mock repository"""
    return QuotaResolver(repository=mock_repository, cache_ttl_seconds=300)


@pytest.fixture
def sample_tier():
    """Create a sample quota tier"""
    return QuotaTier(
        tier_id="premium",
        tier_name="Premium Tier",
        description="Premium users",
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
def sample_user():
    """Create a sample user"""
    return User(
        user_id="test123",
        email="test@example.com",
        name="Test User",
        roles=["Student"]
    )


@pytest.mark.asyncio
async def test_resolve_direct_user_assignment(resolver, mock_repository, sample_tier, sample_user):
    """Test that direct user assignment takes priority"""
    # Setup mock data
    assignment = QuotaAssignment(
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

    mock_repository.query_user_assignment.return_value = assignment
    mock_repository.get_tier.return_value = sample_tier

    # Resolve
    resolved = await resolver.resolve_user_quota(sample_user)

    # Assertions
    assert resolved is not None
    assert resolved.tier.tier_id == "premium"
    assert resolved.matched_by == "direct_user"
    assert resolved.assignment.assignment_id == "assign1"
    assert resolved.user_id == "test123"

    # Verify repository calls
    mock_repository.query_user_assignment.assert_called_once_with("test123")
    mock_repository.get_tier.assert_called_once_with("premium")


@pytest.mark.asyncio
async def test_resolve_fallback_to_role(resolver, mock_repository, sample_user):
    """Test fallback to role assignment when no direct user assignment"""
    # No direct user assignment
    mock_repository.query_user_assignment.return_value = None

    # Setup role assignment
    role_assignment = QuotaAssignment(
        assignment_id="assign2",
        tier_id="student",
        assignment_type=QuotaAssignmentType.JWT_ROLE,
        jwt_role="Student",
        priority=200,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    student_tier = QuotaTier(
        tier_id="student",
        tier_name="Student Tier",
        monthly_cost_limit=100.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.query_role_assignments.return_value = [role_assignment]
    mock_repository.get_tier.return_value = student_tier

    # Resolve
    resolved = await resolver.resolve_user_quota(sample_user)

    # Assertions
    assert resolved is not None
    assert resolved.tier.tier_id == "student"
    assert resolved.matched_by == "jwt_role:Student"
    assert resolved.assignment.assignment_id == "assign2"

    # Verify repository calls
    mock_repository.query_user_assignment.assert_called_once()
    mock_repository.query_role_assignments.assert_called_once_with("Student")


@pytest.mark.asyncio
async def test_resolve_fallback_to_default(resolver, mock_repository, sample_user):
    """Test fallback to default tier when no user or role assignments"""
    # No direct user assignment
    mock_repository.query_user_assignment.return_value = None
    # No role assignments
    mock_repository.query_role_assignments.return_value = []

    # Setup default assignment
    default_assignment = QuotaAssignment(
        assignment_id="default1",
        tier_id="basic",
        assignment_type=QuotaAssignmentType.DEFAULT_TIER,
        priority=100,
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    basic_tier = QuotaTier(
        tier_id="basic",
        tier_name="Basic Tier",
        monthly_cost_limit=50.0,
        action_on_limit="block",
        enabled=True,
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.list_assignments_by_type.return_value = [default_assignment]
    mock_repository.get_tier.return_value = basic_tier

    # Resolve
    resolved = await resolver.resolve_user_quota(sample_user)

    # Assertions
    assert resolved is not None
    assert resolved.tier.tier_id == "basic"
    assert resolved.matched_by == "default_tier"

    # Verify repository calls - list_assignments_by_type is called for email_domain first, then default_tier
    mock_repository.list_assignments_by_type.assert_any_call(
        assignment_type="default_tier",
        enabled_only=True
    )


@pytest.mark.asyncio
async def test_cache_hit(resolver, mock_repository, sample_tier, sample_user):
    """Test that cache reduces DynamoDB calls"""
    # Setup mock data
    assignment = QuotaAssignment(
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

    mock_repository.query_user_assignment.return_value = assignment
    mock_repository.get_tier.return_value = sample_tier

    # First call - cache miss
    resolved1 = await resolver.resolve_user_quota(sample_user)

    # Second call - cache hit (no DB calls)
    resolved2 = await resolver.resolve_user_quota(sample_user)

    # Assertions
    assert resolved1.tier.tier_id == resolved2.tier.tier_id
    assert resolved1.user_id == resolved2.user_id

    # Verify DB was only called once
    assert mock_repository.query_user_assignment.call_count == 1
    assert mock_repository.get_tier.call_count == 1


@pytest.mark.asyncio
async def test_no_quota_configured(resolver, mock_repository, sample_user):
    """Test handling of user with no quota configuration"""
    # No assignments at all
    mock_repository.query_user_assignment.return_value = None
    mock_repository.query_role_assignments.return_value = []
    mock_repository.list_assignments_by_type.return_value = []

    # Resolve
    resolved = await resolver.resolve_user_quota(sample_user)

    # Assertions
    assert resolved is None


@pytest.mark.asyncio
async def test_cache_invalidation_specific_user(resolver, mock_repository, sample_tier, sample_user):
    """Test cache invalidation for specific user"""
    # Setup mock data
    assignment = QuotaAssignment(
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

    mock_repository.query_user_assignment.return_value = assignment
    mock_repository.get_tier.return_value = sample_tier

    # First call - cache miss
    await resolver.resolve_user_quota(sample_user)

    # Invalidate cache for this user
    resolver.invalidate_cache("test123")

    # Second call - cache miss again (DB called again)
    await resolver.resolve_user_quota(sample_user)

    # Verify DB was called twice
    assert mock_repository.query_user_assignment.call_count == 2


@pytest.mark.asyncio
async def test_disabled_assignment_skipped(resolver, mock_repository, sample_user):
    """Test that disabled assignments are skipped"""
    # Setup disabled assignment
    disabled_assignment = QuotaAssignment(
        assignment_id="assign1",
        tier_id="premium",
        assignment_type=QuotaAssignmentType.DIRECT_USER,
        user_id="test123",
        priority=300,
        enabled=False,  # Disabled
        created_at="2025-01-01T00:00:00Z",
        updated_at="2025-01-01T00:00:00Z",
        created_by="admin"
    )

    mock_repository.query_user_assignment.return_value = disabled_assignment
    mock_repository.query_role_assignments.return_value = []
    mock_repository.list_assignments_by_type.return_value = []

    # Resolve
    resolved = await resolver.resolve_user_quota(sample_user)

    # Should return None since assignment is disabled
    assert resolved is None
