"""Tests for SkillAgent's DB-source helpers (PR-6).

Covers the repository fetch + ACTIVE filtering that feeds the registry, plus
the status check — without constructing a full agent (which needs a live model
+ session manager).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents.main_agent import skill_agent


def _rec(skill_id, status="active"):
    return SimpleNamespace(
        skill_id=skill_id,
        description="",
        instructions="body",
        compose=[],
        resources=[],
        status=status,
    )


class TestIsActiveStatus:
    def test_accepts_string_active(self):
        assert skill_agent._is_active_status("active") is True

    def test_accepts_enum_repr_active(self):
        # use_enum_values is on in the model, but be robust to "SkillStatus.ACTIVE".
        assert skill_agent._is_active_status("SkillStatus.ACTIVE") is True

    def test_rejects_non_active(self):
        assert skill_agent._is_active_status("draft") is False
        assert skill_agent._is_active_status("disabled") is False


class TestFetchSkillRecords:
    def test_empty_ids_short_circuit(self):
        assert skill_agent._fetch_skill_records([]) == []

    def test_filters_to_active_records(self):
        repo = MagicMock()
        repo.batch_get_skills = AsyncMock(
            return_value=[
                _rec("active_one", status="active"),
                _rec("draft_one", status="draft"),
                _rec("disabled_one", status="disabled"),
            ]
        )
        # get_skill_catalog_repository is imported inside the function, so
        # patch it at the source module.
        with patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            return_value=repo,
        ):
            records = skill_agent._fetch_skill_records(
                ["active_one", "draft_one", "disabled_one"]
            )

        assert [r.skill_id for r in records] == ["active_one"]

    def test_degrades_to_empty_on_repo_error(self):
        repo = MagicMock()
        repo.batch_get_skills = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            return_value=repo,
        ):
            # Never raises into agent construction — returns [] so the agent
            # degrades to chat.
            assert skill_agent._fetch_skill_records(["x"]) == []
