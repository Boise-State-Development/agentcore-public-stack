"""Tests for the system_prompts shared module (models + repository + service)."""

import pytest

from apis.shared.system_prompts.models import (
    SystemPrompt,
    SystemPromptCreate,
    SystemPromptUpdate,
)
from apis.shared.system_prompts.repository import SystemPromptsRepository
from apis.shared.system_prompts.service import SystemPromptsService

import boto3

AWS_REGION = "us-east-1"


@pytest.fixture()
def system_prompts_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-system-prompts"
    ddb.create_table(
        TableName=name,
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
    monkeypatch.setenv("DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME", name)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture()
def repo(system_prompts_table):
    return SystemPromptsRepository(table_name="test-system-prompts", region=AWS_REGION)


@pytest.fixture()
def service(repo):
    return SystemPromptsService(repo)


def _create_data(**kw) -> SystemPromptCreate:
    defaults = dict(
        name="Guided Learning",
        description="Uses the Socratic method to guide learning.",
        prompt_text="Do not give answers directly. Ask guiding questions.",
        status="enabled",
    )
    defaults.update(kw)
    return SystemPromptCreate(**defaults)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestSystemPromptModel:
    def test_dynamo_roundtrip(self):
        prompt = SystemPrompt(
            prompt_id="abc-123",
            name="Test",
            description="A test prompt",
            prompt_text="Be helpful.",
            status="enabled",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )
        item = prompt.to_dynamo_item()
        assert item["PK"] == "PROMPT#abc-123"
        assert item["SK"] == "METADATA"
        assert item["name"] == "Test"
        assert item["promptText"] == "Be helpful."

        recovered = SystemPrompt.from_dynamo_item(item)
        assert recovered.prompt_id == "abc-123"
        assert recovered.name == "Test"
        assert recovered.prompt_text == "Be helpful."

    def test_from_dynamo_missing_timestamps_raises(self):
        with pytest.raises(ValueError, match="missing required timestamp"):
            SystemPrompt.from_dynamo_item({
                "PK": "PROMPT#x", "SK": "METADATA",
                "promptId": "x", "name": "x", "description": "x", "promptText": "x",
            })

    def test_from_dynamo_unknown_status_falls_back_to_disabled(self):
        """Defensive: unknown status string is treated as disabled, not silently
        re-enabled. This guards against accidentally activating prompts after
        a future migration."""
        recovered = SystemPrompt.from_dynamo_item({
            "PK": "PROMPT#x", "SK": "METADATA",
            "promptId": "x", "name": "x", "description": "x", "promptText": "x",
            "status": "draft",
            "createdAt": "2025-01-01T00:00:00Z",
            "updatedAt": "2025-01-01T00:00:00Z",
        })
        assert recovered.status == "disabled"

    def test_create_rejects_prompt_text_too_long(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SystemPromptCreate(
                name="x",
                description="y",
                prompt_text="z" * 8001,
            )

    def test_create_rejects_invalid_status(self):
        """Literal type means pydantic enforces the status value."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SystemPromptCreate(
                name="x",
                description="y",
                prompt_text="z",
                status="bogus",  # type: ignore[arg-type]
            )

    def test_create_defaults_status_to_enabled(self):
        data = SystemPromptCreate(name="x", description="y", prompt_text="z")
        assert data.status == "enabled"


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------

class TestSystemPromptsRepository:
    @pytest.mark.asyncio
    async def test_create_and_get(self, repo):
        created = await repo.create_prompt(_create_data(), created_by="admin@example.com")
        assert created.prompt_id
        assert created.name == "Guided Learning"
        assert created.status == "enabled"
        assert created.created_by == "admin@example.com"

        fetched = await repo.get_prompt(created.prompt_id)
        assert fetched is not None
        assert fetched.prompt_id == created.prompt_id
        assert fetched.prompt_text == "Do not give answers directly. Ask guiding questions."

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, repo):
        result = await repo.get_prompt("does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_all(self, repo):
        await repo.create_prompt(_create_data(name="A"))
        await repo.create_prompt(_create_data(name="B", status="disabled"))
        results = await repo.list_prompts()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_enabled_only(self, repo):
        await repo.create_prompt(_create_data(name="A"))
        await repo.create_prompt(_create_data(name="B", status="disabled"))
        results = await repo.list_prompts(enabled_only=True)
        assert len(results) == 1
        assert results[0].name == "A"

    @pytest.mark.asyncio
    async def test_list_sorted_alphabetically(self, repo):
        await repo.create_prompt(_create_data(name="Zebra"))
        await repo.create_prompt(_create_data(name="Apple"))
        results = await repo.list_prompts()
        assert results[0].name == "Apple"
        assert results[1].name == "Zebra"

    @pytest.mark.asyncio
    async def test_update_partial(self, repo):
        created = await repo.create_prompt(_create_data())
        updated = await repo.update_prompt(
            created.prompt_id,
            SystemPromptUpdate(status="disabled"),
        )
        assert updated is not None
        assert updated.status == "disabled"
        assert updated.name == "Guided Learning"  # unchanged

    @pytest.mark.asyncio
    async def test_update_missing_returns_none(self, repo):
        result = await repo.update_prompt("no-such-id", SystemPromptUpdate(name="x"))
        assert result is None

    @pytest.mark.asyncio
    async def test_update_does_not_resurrect_deleted(self, repo):
        """If the row is deleted between our read and write, the conditional
        put fails and we return None instead of recreating the deleted row."""
        created = await repo.create_prompt(_create_data())

        # Simulate a concurrent delete: drop the row directly via the table.
        repo._table.delete_item(Key={"PK": f"PROMPT#{created.prompt_id}", "SK": "METADATA"})

        # Patch get_prompt so update sees the pre-delete state but the put
        # races with the missing row.
        original_get = repo.get_prompt

        async def stale_get(prompt_id):
            return created  # pretend it's still there

        repo.get_prompt = stale_get  # type: ignore[method-assign]
        try:
            result = await repo.update_prompt(
                created.prompt_id,
                SystemPromptUpdate(status="disabled"),
            )
        finally:
            repo.get_prompt = original_get  # type: ignore[method-assign]

        assert result is None
        # And the row should still be gone.
        assert await original_get(created.prompt_id) is None

    @pytest.mark.asyncio
    async def test_delete(self, repo):
        created = await repo.create_prompt(_create_data())
        deleted = await repo.delete_prompt(created.prompt_id)
        assert deleted is True
        assert await repo.get_prompt(created.prompt_id) is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, repo):
        result = await repo.delete_prompt("ghost-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_disabled_repo_returns_empty(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME", raising=False)
        repo_no_table = SystemPromptsRepository(table_name=None)
        assert repo_no_table.enabled is False
        assert await repo_no_table.list_prompts() == []
        assert await repo_no_table.get_prompt("x") is None
        assert await repo_no_table.delete_prompt("x") is False


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------

class TestSystemPromptsService:
    @pytest.mark.asyncio
    async def test_get_enabled_prompt_returns_enabled(self, service):
        created = await service.create_prompt(_create_data())
        result = await service.get_enabled_prompt(created.prompt_id)
        assert result is not None
        assert result.prompt_id == created.prompt_id

    @pytest.mark.asyncio
    async def test_get_enabled_prompt_returns_none_for_disabled(self, service):
        created = await service.create_prompt(_create_data(status="disabled"))
        result = await service.get_enabled_prompt(created.prompt_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_enabled_prompt_returns_none_for_missing(self, service):
        assert await service.get_enabled_prompt("no-such-id") is None
