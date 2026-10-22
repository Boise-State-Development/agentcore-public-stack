"""Tests for the agent_templates shared module (models + repository + service + seed)."""

import boto3
import pytest

from apis.shared.agent_templates.models import (
    AgentTemplate,
    AgentTemplateCreate,
    AgentTemplateUpdate,
    BindingPayload,
    ModelConfigPayload,
    TemplateBinding,
    TemplateModelConfig,
)
from apis.shared.agent_templates.repository import (
    AgentTemplatesRepository,
    slugify,
)
from apis.shared.agent_templates.service import AgentTemplatesService
from apis.shared.agent_templates.seed import seed_default_templates
from apis.shared.caching import config_cache

AWS_REGION = "us-east-1"
TABLE_NAME = "test-agent-templates"


@pytest.fixture()
def templates_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("DYNAMODB_AGENT_TEMPLATES_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    # Clear the process-wide config cache between tests so a Scan is re-issued.
    config_cache.invalidate(config_cache.AGENT_TEMPLATES)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


@pytest.fixture()
def repo(templates_table):
    return AgentTemplatesRepository(table_name=TABLE_NAME, region=AWS_REGION)


@pytest.fixture()
def service(repo):
    return AgentTemplatesService(repo)


def _create_data(**kw) -> AgentTemplateCreate:
    defaults = dict(
        template_id="course-helper",
        name="Course Helper",
        description="A study assistant for one course.",
        emoji="🎓",
        instructions="You are a course helper.",
        tags=["demo"],
        starters=["What topics does this cover?"],
        model_cfg=ModelConfigPayload(model_id="us.anthropic.claude-sonnet-5", params={}),
        bindings=[BindingPayload(kind="tool", ref="some_tool", config={})],
        pitch="Answers from your course materials.",
        status="enabled",
        sort_order=10,
    )
    defaults.update(kw)
    return AgentTemplateCreate(**defaults)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestAgentTemplateModel:
    def test_dynamo_roundtrip(self):
        t = AgentTemplate(
            template_id="abc-123",
            name="Test",
            description="A test template",
            emoji="🤖",
            instructions="Be helpful.",
            tags=["a", "b"],
            starters=["Hello?"],
            model_config_=TemplateModelConfig(model_id="model-x", params={"temperature": 1}),
            bindings=[TemplateBinding(kind="tool", ref="t1", config={"k": "v"})],
            pitch="A pitch.",
            status="enabled",
            sort_order=5,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            created_by="admin@example.com",
        )
        item = t.to_dynamo_item()
        assert item["PK"] == "TEMPLATE#abc-123"
        assert item["SK"] == "METADATA"
        assert item["modelConfig"]["modelId"] == "model-x"
        assert item["bindings"][0]["ref"] == "t1"

        back = AgentTemplate.from_dynamo_item(item)
        assert back.template_id == "abc-123"
        assert back.model_config_.model_id == "model-x"
        assert back.bindings[0].kind == "tool"
        assert back.sort_order == 5

    def test_from_item_defaults_missing_status_to_enabled(self):
        t = AgentTemplate.from_dynamo_item(
            {
                "PK": "TEMPLATE#x",
                "SK": "METADATA",
                "templateId": "x",
                "name": "X",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
            }
        )
        assert t.status == "enabled"
        assert t.model_config_.model_id is None
        assert t.bindings == []

    def test_from_item_unknown_status_is_disabled(self):
        t = AgentTemplate.from_dynamo_item(
            {
                "PK": "TEMPLATE#x",
                "SK": "METADATA",
                "templateId": "x",
                "name": "X",
                "status": "weird",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
            }
        )
        assert t.status == "disabled"


class TestSlugify:
    def test_basic(self):
        assert slugify("Course Helper") == "course-helper"

    def test_punctuation_collapses(self):
        assert slugify("Q&A  Assistant!!") == "q-a-assistant"

    def test_empty_falls_back_to_uuid(self):
        assert slugify("   ").startswith("template-")


# ---------------------------------------------------------------------------
# Repository / service tests (moto-backed)
# ---------------------------------------------------------------------------


class TestRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, repo):
        created = await repo.create_template(_create_data(), created_by="admin@example.com")
        assert created.template_id == "course-helper"
        fetched = await repo.get_template("course-helper")
        assert fetched is not None
        assert fetched.name == "Course Helper"
        assert fetched.model_config_.model_id == "us.anthropic.claude-sonnet-5"

    @pytest.mark.asyncio
    async def test_create_slugifies_when_id_omitted(self, repo):
        created = await repo.create_template(
            _create_data(template_id=None, name="My New Template")
        )
        assert created.template_id == "my-new-template"

    @pytest.mark.asyncio
    async def test_duplicate_id_raises(self, repo):
        await repo.create_template(_create_data())
        with pytest.raises(ValueError):
            await repo.create_template(_create_data())

    @pytest.mark.asyncio
    async def test_list_sorts_by_sort_order(self, repo):
        await repo.create_template(_create_data(template_id="b", name="B", sort_order=20))
        await repo.create_template(_create_data(template_id="a", name="A", sort_order=10))
        listed = await repo.list_templates()
        assert [t.template_id for t in listed] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, repo):
        await repo.create_template(_create_data(template_id="on", name="On", status="enabled"))
        await repo.create_template(_create_data(template_id="off", name="Off", status="disabled"))
        listed = await repo.list_templates(enabled_only=True)
        assert [t.template_id for t in listed] == ["on"]

    @pytest.mark.asyncio
    async def test_update_partial(self, repo):
        await repo.create_template(_create_data())
        updated = await repo.update_template(
            "course-helper",
            AgentTemplateUpdate(name="Renamed", status="disabled"),
        )
        assert updated is not None
        assert updated.name == "Renamed"
        assert updated.status == "disabled"
        # Untouched fields survive.
        assert updated.emoji == "🎓"

    @pytest.mark.asyncio
    async def test_update_replaces_nested_bindings_and_model(self, repo):
        await repo.create_template(_create_data())
        updated = await repo.update_template(
            "course-helper",
            AgentTemplateUpdate(
                model_cfg=ModelConfigPayload(model_id=None, params={}),
                bindings=[],
            ),
        )
        assert updated is not None
        assert updated.model_config_.model_id is None
        assert updated.bindings == []

    @pytest.mark.asyncio
    async def test_update_missing_returns_none(self, repo):
        assert await repo.update_template("nope", AgentTemplateUpdate(name="x")) is None

    @pytest.mark.asyncio
    async def test_delete(self, repo):
        await repo.create_template(_create_data())
        assert await repo.delete_template("course-helper") is True
        assert await repo.get_template("course-helper") is None
        assert await repo.delete_template("course-helper") is False


# ---------------------------------------------------------------------------
# Seed tests
# ---------------------------------------------------------------------------


class TestSeed:
    @pytest.mark.asyncio
    async def test_seed_is_idempotent(self, service):
        first = await seed_default_templates(service)
        assert first == 3
        # Re-running seeds nothing new.
        second = await seed_default_templates(service)
        assert second == 0

    @pytest.mark.asyncio
    async def test_seed_templates_are_fork_safe(self, service):
        await seed_default_templates(service)
        templates = await service.list_templates()
        assert len(templates) == 3
        for t in templates:
            # Fork-safety invariants: no org-specific tool refs, platform-default model.
            assert t.bindings == [], f"{t.template_id} must bind no tools"
            assert t.model_config_.model_id is None, f"{t.template_id} must use default model"
