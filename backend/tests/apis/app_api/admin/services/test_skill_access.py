"""Tests for SkillAccessService.

Mirrors test_tool_access.py: filter_allowed_skills sources its "universe of
known skills" from the DynamoDB-backed catalog (via the freshness snapshot).
Unlike tools, skills have no dynamically-loaded ("gateway_*") form, so there
is no prefix-passthrough case.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.admin.services.skill_access import SkillAccessService
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.skills import freshness


@pytest.fixture(autouse=True)
def _reset_freshness():
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def _user(roles=None) -> User:
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin",
        roles=roles or [],
    )


def _permissions(skills, app_roles=("admin",)) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id="admin-1",
        app_roles=list(app_roles),
        tools=[],
        models=[],
        skills=list(skills),
        quota_tier=None,
        resolved_at="2026-06-09T00:00:00Z",
    )


def _service(permissions: UserEffectivePermissions) -> SkillAccessService:
    role_service = AsyncMock()
    role_service.resolve_user_permissions = AsyncMock(return_value=permissions)
    return SkillAccessService(app_role_service=role_service)


def _patch_catalog(skill_ids):
    """Patch the repository.list_skills call that backs the freshness snapshot."""
    repo = SimpleNamespace(
        list_skills=AsyncMock(
            return_value=[SimpleNamespace(skill_id=sid) for sid in skill_ids]
        )
    )
    return patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    )


@pytest.mark.asyncio
async def test_can_access_skill_wildcard():
    service = _service(_permissions(["*"]))
    assert await service.can_access_skill(_user(), "pdf_workflows") is True


@pytest.mark.asyncio
async def test_can_access_skill_specific():
    service = _service(_permissions(["pdf_workflows"]))
    assert await service.can_access_skill(_user(), "pdf_workflows") is True
    assert await service.can_access_skill(_user(), "doc_basics") is False


@pytest.mark.asyncio
async def test_wildcard_user_expands_to_all_catalog_skills():
    service = _service(_permissions(["*"]))

    with _patch_catalog(["pdf_workflows", "doc_basics"]):
        result = await service.filter_allowed_skills(_user(), None)

    assert set(result) == {"pdf_workflows", "doc_basics"}


@pytest.mark.asyncio
async def test_wildcard_user_filters_out_unknown_skill():
    """Wildcard means 'every known skill', not every id a client claims."""
    service = _service(_permissions(["*"]))

    with _patch_catalog(["pdf_workflows"]):
        result = await service.filter_allowed_skills(
            _user(),
            requested_skills=["pdf_workflows", "made_up_skill"],
        )

    assert result == ["pdf_workflows"]


@pytest.mark.asyncio
async def test_non_wildcard_user_sees_intersection_of_granted_and_catalog():
    service = _service(_permissions(["pdf_workflows", "ghost_skill"]))

    with _patch_catalog(["pdf_workflows", "doc_basics"]):
        result = await service.filter_allowed_skills(_user(), None)

    # ghost_skill is granted but not in the catalog → excluded.
    assert set(result) == {"pdf_workflows"}


@pytest.mark.asyncio
async def test_non_wildcard_user_denies_unauthorized_request():
    service = _service(_permissions(["pdf_workflows"]))

    with _patch_catalog(["pdf_workflows", "doc_basics"]):
        result = await service.filter_allowed_skills(
            _user(),
            requested_skills=["pdf_workflows", "doc_basics"],
        )

    assert result == ["pdf_workflows"]


@pytest.mark.asyncio
async def test_check_access_and_filter_reports_denied():
    service = _service(_permissions(["pdf_workflows"]))

    with _patch_catalog(["pdf_workflows", "doc_basics"]):
        allowed, denied = await service.check_access_and_filter(
            _user(),
            requested_skills=["pdf_workflows", "doc_basics"],
        )

    assert allowed == ["pdf_workflows"]
    assert denied == ["doc_basics"]


@pytest.mark.asyncio
async def test_check_access_and_filter_strict_raises():
    service = _service(_permissions(["pdf_workflows"]))

    with _patch_catalog(["pdf_workflows", "doc_basics"]):
        with pytest.raises(ValueError, match="not authorized"):
            await service.check_access_and_filter(
                _user(),
                requested_skills=["doc_basics"],
                strict=True,
            )


@pytest.mark.asyncio
async def test_get_user_allowed_skills():
    service = _service(_permissions(["pdf_workflows", "doc_basics"]))
    assert await service.get_user_allowed_skills(_user()) == {"pdf_workflows", "doc_basics"}
