"""SkillDefinition model: round-trip (to/from dynamo) + skill_id regex.

Pure-model tests — no AWS. Mirrors test_tools_gateway_config.py in spirit.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from apis.shared.skills.models import (
    SkillDefinition,
    SkillStatus,
    SkillVisibility,
)


def _skill(**kw) -> SkillDefinition:
    defaults = dict(
        skill_id="pdf_workflows",
        display_name="PDF Workflows",
        description="Fill, merge and split PDFs.",
        instructions="# PDF Workflows\nUse the bound tools to manipulate PDFs.",
    )
    defaults.update(kw)
    return SkillDefinition(**defaults)


class TestSkillRoundTrip:
    def test_to_dynamo_item_keys_and_camel_case(self):
        skill = _skill(
            bound_tool_ids=["fill_pdf_form", "gateway_weather"],
            compose=["doc_basics"],
            category="document",
            status=SkillStatus.ACTIVE,
            created_by="admin-1",
            updated_by="admin-1",
        )
        item = skill.to_dynamo_item()

        # Primary key pattern mirrors tools (TOOL# -> SKILL#).
        assert item["PK"] == "SKILL#pdf_workflows"
        assert item["SK"] == "METADATA"

        # SkillOwnerIndex (GSI4) keys are populated for the Phase-2 owner query.
        assert item["GSI4PK"] == "OWNER#system"
        assert item["GSI4SK"] == "SKILL#pdf_workflows"

        # snake_case -> camelCase, same convention as ToolDefinition.
        assert item["skillId"] == "pdf_workflows"
        assert item["displayName"] == "PDF Workflows"
        assert item["boundToolIds"] == ["fill_pdf_form", "gateway_weather"]
        assert item["compose"] == ["doc_basics"]
        assert item["status"] == "active"
        assert item["category"] == "document"
        assert item["ownerId"] == "system"
        assert item["visibility"] == "admin"
        assert item["createdBy"] == "admin-1"

        # Computed display-only field is NOT persisted (mirrors tools).
        assert "allowedAppRoles" not in item

    def test_round_trip_preserves_fields(self):
        skill = _skill(
            bound_tool_ids=["fill_pdf_form"],
            compose=["doc_basics"],
            category="document",
            owner_id="user-42",
            visibility=SkillVisibility.PRIVATE,
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        restored = SkillDefinition.from_dynamo_item(skill.to_dynamo_item())

        assert restored.skill_id == "pdf_workflows"
        assert restored.display_name == "PDF Workflows"
        assert restored.description == skill.description
        assert restored.instructions == skill.instructions
        assert restored.bound_tool_ids == ["fill_pdf_form"]
        assert restored.compose == ["doc_basics"]
        assert restored.status == "active"
        assert restored.category == "document"
        assert restored.owner_id == "user-42"
        assert restored.visibility == "private"
        assert restored.created_at == skill.created_at
        assert restored.updated_at == skill.updated_at

    def test_defaults(self):
        skill = _skill()
        assert skill.bound_tool_ids == []
        assert skill.compose == []
        assert skill.status == "active"
        assert skill.category is None
        assert skill.owner_id == "system"
        assert skill.visibility == "admin"
        assert skill.allowed_app_roles == []

    def test_from_dynamo_item_tolerates_missing_optional_fields(self):
        # An older/minimal row with only the required identity fields.
        item = {
            "skillId": "minimal_skill",
            "displayName": "Minimal",
            "description": "desc",
            "instructions": "body",
        }
        restored = SkillDefinition.from_dynamo_item(item)
        assert restored.skill_id == "minimal_skill"
        assert restored.bound_tool_ids == []
        assert restored.owner_id == "system"
        assert restored.visibility == "admin"
        assert isinstance(restored.created_at, datetime)


class TestSkillIdRegex:
    @pytest.mark.parametrize(
        "skill_id",
        ["abc", "pdf_workflows", "a12", "skill_1_2_3", "a" * 50],
    )
    def test_valid_skill_ids(self, skill_id):
        assert _skill(skill_id=skill_id).skill_id == skill_id

    @pytest.mark.parametrize(
        "skill_id",
        [
            "ab",  # too short (<3)
            "1skill",  # must start with a letter
            "Skill",  # no uppercase
            "skill-1",  # no hyphen
            "skill 1",  # no space
            "a" * 51,  # too long (>50)
            "",  # empty
        ],
    )
    def test_invalid_skill_ids(self, skill_id):
        with pytest.raises(ValidationError):
            _skill(skill_id=skill_id)
