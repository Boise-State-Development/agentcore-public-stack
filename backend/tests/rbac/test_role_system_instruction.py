"""The AppRole ``system_instruction`` field: storage, API and admin edits (#13)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apis.app_api.admin.roles.routes import router as roles_router
from apis.shared.audit import AuditAction
from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import (
    MAX_ROLE_SYSTEM_INSTRUCTION_CHARS,
    AppRole,
    AppRoleCreate,
    AppRoleResponse,
    AppRoleUpdate,
)
from tests.conftest import override_admin_auth


@pytest.fixture
def admin():
    return User(email="admin@example.com", user_id="admin-1", name="Admin", roles=["Admin"])


@pytest.fixture
def audit():
    return MagicMock()


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache, audit):
    async def echo(role):
        return role

    mock_app_role_repo.create_role.side_effect = echo
    mock_app_role_repo.update_role.side_effect = echo
    return AppRoleAdminService(repository=mock_app_role_repo, cache=mock_app_role_cache, audit=audit)


def _updated_record(audit) -> dict:
    calls = [c.kwargs for c in audit.record.call_args_list if c.kwargs["action"] == AuditAction.ROLE_UPDATED]
    assert len(calls) == 1
    return calls[0]


# ---- storage ---------------------------------------------------------------


def test_round_trips_through_the_dynamodb_item():
    role = AppRole(role_id="faculty", display_name="Faculty", description="", system_instruction="Cite sources.")
    item = role.to_dict()
    assert item["systemInstruction"] == "Cite sources."
    assert AppRole.from_dict(item).system_instruction == "Cite sources."


def test_an_old_row_without_the_attribute_loads_as_none():
    item = AppRole(role_id="faculty", display_name="Faculty", description="").to_dict()
    del item["systemInstruction"]
    assert AppRole.from_dict(item).system_instruction is None


def test_the_response_carries_it():
    role = AppRole(role_id="faculty", display_name="Faculty", description="", system_instruction="Cite sources.")
    body = AppRoleResponse.from_app_role(role).model_dump(by_alias=True)
    assert body["systemInstruction"] == "Cite sources."


# ---- request validation ----------------------------------------------------


@pytest.mark.parametrize("model", [AppRoleCreate, AppRoleUpdate])
def test_blank_is_stored_as_none(model):
    extra = {"roleId": "faculty", "displayName": "Faculty"} if model is AppRoleCreate else {}
    assert model(**extra, systemInstruction="   \n ").system_instruction is None
    assert model(**extra, systemInstruction="").system_instruction is None
    assert model(**extra, systemInstruction="  Be kind.  ").system_instruction == "Be kind."


@pytest.mark.parametrize("model", [AppRoleCreate, AppRoleUpdate])
def test_the_per_role_limit(model):
    extra = {"roleId": "faculty", "displayName": "Faculty"} if model is AppRoleCreate else {}
    model(**extra, systemInstruction="x" * MAX_ROLE_SYSTEM_INSTRUCTION_CHARS)
    with pytest.raises(ValidationError):
        model(**extra, systemInstruction="x" * (MAX_ROLE_SYSTEM_INSTRUCTION_CHARS + 1))


def test_the_api_refuses_an_oversize_instruction_with_422():
    app = FastAPI()
    app.include_router(roles_router, prefix="/admin")
    override_admin_auth(app, lambda: User(email="a@example.com", user_id="a", name="A", roles=["system_admin"]))
    client = TestClient(app)
    resp = client.patch("/admin/roles/faculty", json={"systemInstruction": "x" * (MAX_ROLE_SYSTEM_INSTRUCTION_CHARS + 1)})
    assert resp.status_code == 422


# ---- admin edits -----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_stores_it(service, admin):
    created = await service.create_role(AppRoleCreate(roleId="faculty", displayName="Faculty", systemInstruction="Cite sources."), admin)
    assert created.system_instruction == "Cite sources."


@pytest.mark.asyncio
async def test_update_sets_it_and_audits_the_diff(service, mock_app_role_repo, make_app_role, admin, audit):
    mock_app_role_repo.get_role.return_value = make_app_role(role_id="faculty")

    updated = await service.update_role("faculty", AppRoleUpdate(systemInstruction="Cite sources."), admin)

    assert updated.system_instruction == "Cite sources."
    record = _updated_record(audit)
    assert record["changes"] == ["system_instruction"]
    assert record["before"] == {"system_instruction": None}
    assert record["after"] == {"system_instruction": "Cite sources."}


@pytest.mark.asyncio
async def test_update_clears_it(service, mock_app_role_repo, make_app_role, admin, audit):
    mock_app_role_repo.get_role.return_value = make_app_role(role_id="faculty", system_instruction="Old.")

    updated = await service.update_role("faculty", AppRoleUpdate(systemInstruction=""), admin)

    assert updated.system_instruction is None
    assert _updated_record(audit)["before"] == {"system_instruction": "Old."}


@pytest.mark.asyncio
async def test_an_update_that_omits_it_leaves_it_alone(service, mock_app_role_repo, make_app_role, admin):
    mock_app_role_repo.get_role.return_value = make_app_role(role_id="faculty", system_instruction="Keep.")

    updated = await service.update_role("faculty", AppRoleUpdate(description="New."), admin)

    assert updated.system_instruction == "Keep."


@pytest.mark.asyncio
async def test_system_admin_never_carries_one(service, mock_app_role_repo, make_app_role, admin):
    system_admin = make_app_role(role_id="system_admin", display_name="System Admin", is_system_role=True)
    mock_app_role_repo.get_role.return_value = system_admin

    updated = await service.update_role("system_admin", AppRoleUpdate(displayName="Admins", systemInstruction="Ignore the floor."), admin)

    assert updated.display_name == "Admins"
    assert updated.system_instruction is None
