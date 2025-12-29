"""Unit tests for ModelAccessService

Tests the hybrid model access checking that supports both:
- AppRole-based access (via allowedAppRoles)
- Legacy JWT role-based access (via availableToRoles)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from apis.app_api.admin.services.model_access import ModelAccessService
from apis.app_api.admin.models import ManagedModel
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions


def create_test_user(
    user_id: str = "test-user-123",
    email: str = "test@example.com",
    roles: list = None,
) -> User:
    """Create a test user with specified roles."""
    return User(
        user_id=user_id,
        email=email,
        name="Test User",
        roles=roles or [],
    )


def create_test_model(
    model_id: str = "test-model",
    enabled: bool = True,
    allowed_app_roles: list = None,
    available_to_roles: list = None,
) -> ManagedModel:
    """Create a test ManagedModel."""
    now = datetime.now(timezone.utc)
    return ManagedModel(
        id="uuid-123",
        model_id=model_id,
        model_name="Test Model",
        provider="bedrock",
        provider_name="AWS Bedrock",
        input_modalities=["TEXT"],
        output_modalities=["TEXT"],
        max_input_tokens=100000,
        max_output_tokens=4096,
        allowed_app_roles=allowed_app_roles or [],
        available_to_roles=available_to_roles or [],
        enabled=enabled,
        input_price_per_million_tokens=3.0,
        output_price_per_million_tokens=15.0,
        is_reasoning_model=False,
        supports_caching=True,
        created_at=now,
        updated_at=now,
    )


class TestModelAccessService:
    """Test suite for ModelAccessService"""

    @pytest.fixture
    def mock_app_role_service(self):
        """Create a mock AppRoleService."""
        mock = AsyncMock()
        return mock

    @pytest.fixture
    def service(self, mock_app_role_service):
        """Create a ModelAccessService with mocked dependencies."""
        return ModelAccessService(app_role_service=mock_app_role_service)

    @pytest.mark.asyncio
    async def test_disabled_model_returns_false(self, service):
        """Test that disabled models are never accessible."""
        user = create_test_user(roles=["Admin"])
        model = create_test_model(
            enabled=False,
            allowed_app_roles=["admin"],
            available_to_roles=["Admin"],
        )

        result = await service.can_access_model(user, model)

        assert result is False

    @pytest.mark.asyncio
    async def test_app_role_wildcard_access(self, service, mock_app_role_service):
        """Test that wildcard (*) in AppRole permissions grants access."""
        user = create_test_user(roles=["Faculty"])
        model = create_test_model(allowed_app_roles=["power_user"])

        # Mock that user has wildcard model access
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["power_user"],
                tools=[],
                models=["*"],  # Wildcard access
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is True
        mock_app_role_service.resolve_user_permissions.assert_called_once_with(user)

    @pytest.mark.asyncio
    async def test_app_role_specific_model_access(self, service, mock_app_role_service):
        """Test that specific model_id in AppRole permissions grants access."""
        user = create_test_user(roles=["Faculty"])
        model = create_test_model(
            model_id="claude-opus",
            allowed_app_roles=["power_user"],
        )

        # Mock that user has access to this specific model
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["power_user"],
                tools=[],
                models=["claude-opus", "claude-sonnet"],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is True

    @pytest.mark.asyncio
    async def test_app_role_no_access(self, service, mock_app_role_service):
        """Test that missing model_id in AppRole permissions denies access."""
        user = create_test_user(roles=["Faculty"])
        model = create_test_model(
            model_id="gpt-4o",
            allowed_app_roles=["power_user"],
        )

        # Mock that user has access to different models
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["basic_user"],
                tools=[],
                models=["claude-sonnet"],  # Does not include gpt-4o
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is False

    @pytest.mark.asyncio
    async def test_legacy_jwt_role_access(self, service, mock_app_role_service):
        """Test that legacy JWT role matching grants access."""
        user = create_test_user(roles=["Faculty", "Researcher"])
        model = create_test_model(
            model_id="claude-opus",
            allowed_app_roles=[],  # No AppRole config
            available_to_roles=["Faculty", "Staff"],  # Legacy JWT roles
        )

        # Mock that user has no AppRole model access
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=[],
                tools=[],
                models=[],  # No model access via AppRole
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        # Should still get access via legacy JWT role
        assert result is True

    @pytest.mark.asyncio
    async def test_legacy_jwt_role_no_match(self, service, mock_app_role_service):
        """Test that non-matching JWT roles deny access."""
        user = create_test_user(roles=["Student"])
        model = create_test_model(
            model_id="claude-opus",
            allowed_app_roles=[],
            available_to_roles=["Faculty", "Staff"],  # Student not included
        )

        # Mock that user has no AppRole model access
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=[],
                tools=[],
                models=[],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is False

    @pytest.mark.asyncio
    async def test_hybrid_access_app_role_first(self, service, mock_app_role_service):
        """Test that AppRole access is checked first, then JWT roles."""
        user = create_test_user(roles=["Faculty"])
        model = create_test_model(
            model_id="claude-opus",
            allowed_app_roles=["power_user"],
            available_to_roles=["Faculty"],  # Would also match
        )

        # Mock that user has AppRole access
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["power_user"],
                tools=[],
                models=["claude-opus"],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is True
        # AppRole check should have been the path taken

    @pytest.mark.asyncio
    async def test_no_access_config_returns_false(self, service, mock_app_role_service):
        """Test that models with no access config deny all users."""
        user = create_test_user(roles=["Admin"])
        model = create_test_model(
            model_id="claude-opus",
            allowed_app_roles=[],
            available_to_roles=[],
        )

        # Mock that user has no AppRole model access
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["system_admin"],
                tools=[],
                models=[],  # Admin doesn't have model access configured
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.can_access_model(user, model)

        assert result is False


class TestModelAccessServiceFilterModels:
    """Test suite for filter_accessible_models method"""

    @pytest.fixture
    def mock_app_role_service(self):
        """Create a mock AppRoleService."""
        mock = AsyncMock()
        return mock

    @pytest.fixture
    def service(self, mock_app_role_service):
        """Create a ModelAccessService with mocked dependencies."""
        return ModelAccessService(app_role_service=mock_app_role_service)

    @pytest.mark.asyncio
    async def test_filter_with_wildcard_access(self, service, mock_app_role_service):
        """Test that wildcard access returns all enabled models."""
        user = create_test_user(roles=["Admin"])
        models = [
            create_test_model(model_id="model-1", enabled=True),
            create_test_model(model_id="model-2", enabled=True),
            create_test_model(model_id="model-3", enabled=False),  # Disabled
        ]

        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["system_admin"],
                tools=["*"],
                models=["*"],  # Wildcard
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.filter_accessible_models(user, models)

        assert len(result) == 2
        assert all(m.model_id in ["model-1", "model-2"] for m in result)

    @pytest.mark.asyncio
    async def test_filter_mixed_access(self, service, mock_app_role_service):
        """Test filtering with mixed AppRole and JWT role access."""
        user = create_test_user(roles=["Faculty"])
        models = [
            create_test_model(
                model_id="model-1",
                enabled=True,
                allowed_app_roles=["power_user"],
            ),
            create_test_model(
                model_id="model-2",
                enabled=True,
                available_to_roles=["Faculty"],  # JWT role match
            ),
            create_test_model(
                model_id="model-3",
                enabled=True,
                available_to_roles=["Staff"],  # No match
            ),
        ]

        # User has access to model-1 via AppRole
        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["power_user"],
                tools=[],
                models=["model-1"],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.filter_accessible_models(user, models)

        assert len(result) == 2
        model_ids = [m.model_id for m in result]
        assert "model-1" in model_ids  # AppRole access
        assert "model-2" in model_ids  # JWT role access
        assert "model-3" not in model_ids  # No access

    @pytest.mark.asyncio
    async def test_filter_empty_models_list(self, service, mock_app_role_service):
        """Test filtering an empty list returns empty list."""
        user = create_test_user(roles=["Admin"])

        mock_app_role_service.resolve_user_permissions.return_value = (
            UserEffectivePermissions(
                user_id=user.user_id,
                app_roles=["system_admin"],
                tools=["*"],
                models=["*"],
                quota_tier=None,
                resolved_at=datetime.utcnow().isoformat() + "Z",
            )
        )

        result = await service.filter_accessible_models(user, [])

        assert result == []

    @pytest.mark.asyncio
    async def test_filter_permissions_error_fallback(
        self, service, mock_app_role_service
    ):
        """Test that JWT role fallback works when AppRole service errors."""
        user = create_test_user(roles=["Faculty"])
        models = [
            create_test_model(
                model_id="model-1",
                enabled=True,
                available_to_roles=["Faculty"],
            ),
        ]

        # Simulate error in AppRole service
        mock_app_role_service.resolve_user_permissions.side_effect = Exception(
            "Service unavailable"
        )

        result = await service.filter_accessible_models(user, models)

        # Should still work via JWT role fallback
        assert len(result) == 1
        assert result[0].model_id == "model-1"
