"""Unit tests for ModelRoleService.

The AppRole record is the single source of truth for model access. These tests
pin the two directions of that contract:

* the model form's role picker writes THROUGH to each role's ``grantedModels``
  (previously it wrote a model-side field that no access check ever read, so
  enabling a model for a role from the model page silently did nothing);
* the model's role fields are derived back FROM the roles on read, so the model
  page and the role page can never show different answers.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from apis.app_api.admin.services.model_roles import ModelRoleService
from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.rbac.models import AppRole


def make_admin() -> User:
    return User(
        user_id="admin-1", email="admin@example.com", name="Admin", roles=["Admin"]
    )


def make_role(
    role_id: str,
    granted_models: list = None,
    inherits_from: list = None,
    enabled: bool = True,
) -> AppRole:
    return AppRole(
        role_id=role_id,
        display_name=role_id.title(),
        description=f"{role_id} role",
        granted_models=granted_models or [],
        inherits_from=inherits_from or [],
        enabled=enabled,
    )


def make_model(model_id: str = "claude-sonnet-5") -> ManagedModel:
    now = datetime.now(timezone.utc)
    return ManagedModel(
        id="uuid-1",
        model_id=model_id,
        model_name="Claude Sonnet 5",
        provider="bedrock",
        provider_name="AWS Bedrock",
        input_modalities=["TEXT"],
        output_modalities=["TEXT"],
        max_input_tokens=200000,
        enabled=True,
        input_price_per_million_tokens=3.0,
        output_price_per_million_tokens=15.0,
        created_at=now,
        updated_at=now,
    )


def make_service(roles: list) -> tuple:
    """Build a ModelRoleService over a fixed role list. Returns (service, admin_mock)."""
    admin_service = AsyncMock()
    admin_service.list_roles.return_value = roles
    return ModelRoleService(app_role_admin_service=admin_service), admin_service


def granted_models_written(admin_service, role_id: str) -> list:
    """The grantedModels passed to update_role for a given role."""
    for call in admin_service.update_role.await_args_list:
        if call.args[0] == role_id:
            return call.args[1].granted_models
    raise AssertionError(f"update_role was never called for {role_id}")


class TestSetRolesForModel:
    """The write-through: the picker's selection lands on the ROLE records."""

    @pytest.mark.asyncio
    async def test_grant_adds_model_to_role_granted_models(self):
        """
        The bug this fixes: enabling Sonnet 5 for Staff on the model page must
        add the model to the Staff role's grantedModels — that is the only field
        the chat model list reads.
        """
        service, admin_service = make_service([make_role("staff")])

        await service.set_roles_for_model("claude-sonnet-5", ["staff"], make_admin())

        assert granted_models_written(admin_service, "staff") == ["claude-sonnet-5"]

    @pytest.mark.asyncio
    async def test_deselecting_a_role_revokes_the_grant(self):
        service, admin_service = make_service(
            [make_role("staff", granted_models=["claude-sonnet-5", "other"])]
        )

        await service.set_roles_for_model("claude-sonnet-5", [], make_admin())

        assert granted_models_written(admin_service, "staff") == ["other"]

    @pytest.mark.asyncio
    async def test_untouched_roles_are_not_rewritten(self):
        """Only roles whose grants actually change should be written."""
        service, admin_service = make_service(
            [
                make_role("staff", granted_models=["claude-sonnet-5"]),
                make_role("student", granted_models=["haiku"]),
            ]
        )

        await service.set_roles_for_model("claude-sonnet-5", ["staff"], make_admin())

        admin_service.update_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wildcard_role_is_not_given_a_redundant_grant(self):
        """A role granting '*' already covers every model; don't add the id too."""
        service, admin_service = make_service(
            [make_role("system_admin", granted_models=["*"])]
        )

        await service.set_roles_for_model(
            "claude-sonnet-5", ["system_admin"], make_admin()
        )

        admin_service.update_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_role_is_rejected(self):
        service, admin_service = make_service([make_role("staff")])

        with pytest.raises(ValueError, match="Unknown AppRole"):
            await service.set_roles_for_model("claude-sonnet-5", ["ghost"], make_admin())

        admin_service.update_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rename_migrates_grants_to_the_new_model_id(self):
        """Roles key grants on the provider model id, so a rename must move them."""
        service, admin_service = make_service(
            [make_role("staff", granted_models=["old-id", "other"])]
        )

        await service.set_roles_for_model(
            "new-id", ["staff"], make_admin(), previous_model_id="old-id"
        )

        written = granted_models_written(admin_service, "staff")
        assert "old-id" not in written
        assert written == ["other", "new-id"]


class TestRevokeModelFromAllRoles:
    @pytest.mark.asyncio
    async def test_delete_strips_the_model_from_every_role(self):
        service, admin_service = make_service(
            [
                make_role("staff", granted_models=["claude-sonnet-5", "haiku"]),
                make_role("student", granted_models=["claude-sonnet-5"]),
                make_role("guest", granted_models=["haiku"]),
            ]
        )

        await service.revoke_model_from_all_roles("claude-sonnet-5", make_admin())

        assert granted_models_written(admin_service, "staff") == ["haiku"]
        assert granted_models_written(admin_service, "student") == []
        # 'guest' never granted it, so it should not be rewritten.
        assert admin_service.update_role.await_count == 2


class TestHydrateModelRoles:
    """The derived read: role records -> the model's displayed role fields."""

    @pytest.mark.asyncio
    async def test_direct_grants_populate_allowed_app_roles(self):
        service, _ = make_service(
            [
                make_role("staff", granted_models=["claude-sonnet-5"]),
                make_role("student", granted_models=["haiku"]),
            ]
        )
        model = make_model()

        await service.hydrate_model_roles([model])

        assert model.allowed_app_roles == ["staff"]
        assert model.inherited_app_roles == []

    @pytest.mark.asyncio
    async def test_wildcard_and_inherited_grants_are_reported_separately(self):
        """
        A wildcard or inherited grant is real access, but it isn't a direct grant
        — it must not show up as a checked box the admin could 'uncheck'.
        """
        service, _ = make_service(
            [
                make_role("system_admin", granted_models=["*"]),
                make_role("staff", granted_models=["claude-sonnet-5"]),
                make_role("ta", inherits_from=["staff"]),
            ]
        )
        model = make_model()

        await service.hydrate_model_roles([model])

        assert model.allowed_app_roles == ["staff"]
        assert sorted(model.inherited_app_roles) == ["system_admin", "ta"]

    @pytest.mark.asyncio
    async def test_inheritance_from_a_disabled_parent_does_not_grant(self):
        service, _ = make_service(
            [
                make_role("staff", granted_models=["claude-sonnet-5"], enabled=False),
                make_role("ta", inherits_from=["staff"]),
            ]
        )
        model = make_model()

        await service.hydrate_model_roles([model])

        assert model.inherited_app_roles == []

    @pytest.mark.asyncio
    async def test_hydrating_a_catalog_queries_roles_once(self):
        """Cost guard: one role query for the whole model list, not one per model."""
        service, admin_service = make_service(
            [make_role("staff", granted_models=["m1"])]
        )
        models = [make_model("m1"), make_model("m2"), make_model("m3")]

        await service.hydrate_model_roles(models)

        admin_service.list_roles.assert_awaited_once()
        assert models[0].allowed_app_roles == ["staff"]
        assert models[1].allowed_app_roles == []
