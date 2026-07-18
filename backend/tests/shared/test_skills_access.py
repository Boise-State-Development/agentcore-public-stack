"""Unit tests for the shared per-user skill access resolver."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.auth.models import User
from apis.shared.skills.access import resolve_accessible_skill_ids

pytestmark = pytest.mark.asyncio


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="tok",
    )


class TestResolveAccessibleSkillIds:
    async def test_plain_grants_pass_through(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(
            return_value=["web_research", "pdf_workflows"]
        )
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == ["web_research", "pdf_workflows"]

    async def test_wildcard_expands_to_all_known_skills_sorted(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["*"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), patch(
            "apis.shared.skills.freshness.get_all_skill_ids",
            AsyncMock(return_value=frozenset({"zeta", "alpha"})),
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == ["alpha", "zeta"]

    async def test_failure_degrades_to_no_skills(self):
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            side_effect=RuntimeError("rbac unavailable"),
        ):
            result = await resolve_accessible_skill_ids(_user())
        assert result == []


def _owned(*skill_ids):
    """Patch the owner-index query to return skills with these ids."""
    records = [MagicMock(skill_id=sid) for sid in skill_ids]
    repo = MagicMock()
    repo.list_skills_by_owner = AsyncMock(return_value=records)
    return patch(
        "apis.shared.skills.repository.get_skill_catalog_repository",
        return_value=repo,
    )


class TestOwnedSkillUnion:
    """Ownership is its own grant — a user always reaches skills they authored."""

    async def test_owned_skills_are_unioned_onto_catalog_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["pdf_workflows"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["pdf_workflows", "my_notes"]

    async def test_owned_skills_reach_a_user_with_no_role_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=[])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["my_notes"]

    async def test_a_skill_both_granted_and_owned_appears_once(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["my_notes"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), _owned("my_notes"):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["my_notes"]

    async def test_owner_lookup_failure_still_yields_catalog_grants(self):
        role_service = MagicMock()
        role_service.get_accessible_skills = AsyncMock(return_value=["pdf_workflows"])
        with patch(
            "apis.shared.rbac.service.get_app_role_service",
            return_value=role_service,
        ), patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            side_effect=RuntimeError("dynamo down"),
        ):
            result = await resolve_accessible_skill_ids(_user())

        assert result == ["pdf_workflows"]
