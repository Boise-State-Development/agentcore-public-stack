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
