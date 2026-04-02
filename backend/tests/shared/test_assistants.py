"""Task 12: Assistants service tests (moto DynamoDB)."""

import pytest


class TestAssistantsService:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_create_and_get(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="My Bot",
            description="A test bot", instructions="You are helpful.",
        )
        assert created.name == "My Bot"
        assert created.status == "COMPLETE"
        result = await get_assistant(created.assistant_id, "u1")
        assert result is not None
        assert result.name == "My Bot"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, assistants_table):
        from apis.shared.assistants.service import get_assistant
        assert await get_assistant("nope", "u1") is None

    @pytest.mark.asyncio
    async def test_create_draft(self, assistants_table):
        from apis.shared.assistants.service import create_assistant_draft
        draft = await create_assistant_draft(owner_id="u1", owner_name="Alice")
        assert draft.status == "DRAFT"
        assert draft.owner_id == "u1"

    @pytest.mark.asyncio
    async def test_assistant_exists(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, assistant_exists
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await assistant_exists(created.assistant_id) is True
        assert await assistant_exists("nope") is False

    @pytest.mark.asyncio
    async def test_update_assistant(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, update_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        updated = await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1", name="Updated Bot",
        )
        assert updated.name == "Updated Bot"

    @pytest.mark.asyncio
    async def test_list_user_assistants(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, list_user_assistants
        for i in range(3):
            await create_assistant(
                owner_id="u1", owner_name="Alice", name=f"Bot {i}",
                description="d", instructions="hi",
            )
        assistants, _ = await list_user_assistants(owner_id="u1")
        assert len(assistants) == 3

    @pytest.mark.asyncio
    async def test_delete_assistant(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, delete_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await delete_assistant(created.assistant_id, "u1") is True

    @pytest.mark.asyncio
    async def test_share_and_check_access(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, share_assistant, check_share_access
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await share_assistant(created.assistant_id, "u1", ["bob@example.com"]) is True
        assert await check_share_access(created.assistant_id, "bob@example.com") is True
        assert await check_share_access(created.assistant_id, "eve@example.com") is False

    @pytest.mark.asyncio
    async def test_unshare(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, share_assistant, unshare_assistant, check_share_access
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"])
        await unshare_assistant(created.assistant_id, "u1", ["bob@example.com"])
        assert await check_share_access(created.assistant_id, "bob@example.com") is False

    @pytest.mark.asyncio
    async def test_list_shared_with_user(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, share_assistant, list_shared_with_user
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"])
        shared = await list_shared_with_user("bob@example.com")
        assert len(shared) == 1


class TestRAGService:
    def test_augment_prompt_with_context(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        chunks = [{"text": "Paris is the capital of France.", "score": 0.9}]
        result = augment_prompt_with_context("What is the capital of France?", chunks)
        assert "Paris" in result

    def test_augment_prompt_no_context(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        result = augment_prompt_with_context("Hello", [])
        assert result == "Hello"
