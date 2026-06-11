"""Unit tests for per-user skill preferences (model + repository)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.skills.models import UserSkillPreference
from apis.shared.skills.repository import SkillCatalogRepository

pytestmark = pytest.mark.asyncio


class TestUserSkillPreferenceModel:
    def test_dynamo_round_trip(self):
        original = UserSkillPreference(
            user_id="user-1",
            skill_preferences={"web_research": False, "pdf_workflows": True},
            updated_at=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        )
        item = original.to_dynamo_item()
        assert item["PK"] == "USER#user-1"
        assert item["SK"] == "SKILL_PREFERENCES"

        restored = UserSkillPreference.from_dynamo_item(item)
        assert restored.user_id == "user-1"
        assert restored.skill_preferences == {
            "web_research": False,
            "pdf_workflows": True,
        }

    def test_defaults_to_empty_preferences(self):
        pref = UserSkillPreference(user_id="user-1")
        assert pref.skill_preferences == {}


class TestSkillPreferencesRepository:
    def _make_repo(self) -> SkillCatalogRepository:
        with patch("apis.shared.skills.repository.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_resource = MagicMock()
            mock_resource.Table.return_value = mock_table
            mock_boto.resource.return_value = mock_resource

            repo = SkillCatalogRepository(table_name="test-table")
            repo._table = mock_table
            return repo

    async def test_get_returns_empty_when_no_row(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {}

        result = await repo.get_user_preferences("user-1")
        assert result.user_id == "user-1"
        assert result.skill_preferences == {}

        repo._table.get_item.assert_called_once_with(
            Key={"PK": "USER#user-1", "SK": "SKILL_PREFERENCES"}
        )

    async def test_get_parses_stored_row(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {
            "Item": {
                "PK": "USER#user-1",
                "SK": "SKILL_PREFERENCES",
                "userId": "user-1",
                "skillPreferences": {"web_research": False},
                "updatedAt": "2026-06-11T12:00:00+00:00",
            }
        }

        result = await repo.get_user_preferences("user-1")
        assert result.skill_preferences == {"web_research": False}

    async def test_save_merges_with_existing(self):
        repo = self._make_repo()
        repo._table.get_item.return_value = {
            "Item": {
                "PK": "USER#user-1",
                "SK": "SKILL_PREFERENCES",
                "userId": "user-1",
                "skillPreferences": {"web_research": False},
                "updatedAt": "2026-06-11T12:00:00+00:00",
            }
        }

        result = await repo.save_user_preferences(
            "user-1", {"pdf_workflows": True}
        )

        assert result.skill_preferences == {
            "web_research": False,
            "pdf_workflows": True,
        }
        saved_item = repo._table.put_item.call_args.kwargs["Item"]
        assert saved_item["skillPreferences"] == {
            "web_research": False,
            "pdf_workflows": True,
        }
