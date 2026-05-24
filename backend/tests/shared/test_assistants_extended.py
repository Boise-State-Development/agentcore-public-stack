"""Extended assistants service + RAG tests for deeper coverage."""

import pytest
from unittest.mock import patch, AsyncMock


class TestAssistantsServiceExtended:
    @pytest.fixture(autouse=True)
    def _set_env(self, assistants_table, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_get_assistant_with_access_check_owner(self):
        from apis.shared.assistants.service import create_assistant, get_assistant_with_access_check
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assistant, permission = await get_assistant_with_access_check(created.assistant_id, "u1", "alice@test.com")
        assert assistant is not None
        assert permission == "owner"

    @pytest.mark.asyncio
    async def test_get_assistant_with_access_check_not_owner(self):
        from apis.shared.assistants.service import create_assistant, get_assistant_with_access_check
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assistant, permission = await get_assistant_with_access_check(created.assistant_id, "u2", "bob@test.com")
        assert assistant is None  # private, not shared
        assert permission is None

    @pytest.mark.asyncio
    async def test_list_user_assistants_pagination(self):
        from apis.shared.assistants.service import create_assistant, list_user_assistants
        for i in range(5):
            await create_assistant(
                owner_id="u1", owner_name="Alice", name=f"Bot {i}",
                description="d", instructions="hi",
            )
        assistants, token = await list_user_assistants(owner_id="u1", limit=3)
        assert len(assistants) == 3

    @pytest.mark.asyncio
    async def test_list_assistant_shares(self):
        from apis.shared.assistants.service import create_assistant, share_assistant, list_assistant_shares
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["a@test.com", "b@test.com"])
        shares = await list_assistant_shares(created.assistant_id, "u1")
        assert len(shares) == 2
        # Each share is now a dict with email + permission (default viewer)
        emails = {s["email"] for s in shares}
        assert emails == {"a@test.com", "b@test.com"}
        assert all(s["permission"] == "viewer" for s in shares)

    @pytest.mark.asyncio
    async def test_create_with_tags_and_starters(self):
        from apis.shared.assistants.service import create_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
            tags=["python", "ai"], starters=["Hello!", "Help me"],
        )
        assert created.tags == ["python", "ai"]
        assert created.starters == ["Hello!", "Help me"]

    @pytest.mark.asyncio
    async def test_create_with_visibility(self):
        from apis.shared.assistants.service import create_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi", visibility="PUBLIC",
        )
        assert created.visibility == "PUBLIC"

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self):
        from apis.shared.assistants.service import create_assistant, update_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        updated = await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1",
            name="New Name", description="New desc",
        )
        assert updated.name == "New Name"
        assert updated.description == "New desc"


class TestRAGServiceExtended:
    def test_augment_prompt_truncation(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        chunks = [{"text": "A" * 3000, "score": 0.9}]
        result = augment_prompt_with_context("Q?", chunks, max_context_length=100)
        assert "Q?" in result
        assert "..." in result  # truncated

    def test_augment_prompt_multiple_chunks(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        chunks = [
            {"text": "Chunk 1 content", "score": 0.9},
            {"text": "Chunk 2 content", "score": 0.8},
        ]
        result = augment_prompt_with_context("Q?", chunks)
        assert "[Context 1]" in result
        assert "[Context 2]" in result

    def test_augment_prompt_empty_text_chunks(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        chunks = [{"text": "", "score": 0.9}, {"text": "   ", "score": 0.8}]
        result = augment_prompt_with_context("Q?", chunks)
        assert result == "Q?"  # no valid chunks

    def test_augment_prompt_max_length_boundary(self):
        from apis.shared.assistants.rag_service import augment_prompt_with_context
        chunks = [
            {"text": "A" * 50, "score": 0.9},
            {"text": "B" * 50, "score": 0.8},
            {"text": "C" * 50, "score": 0.7},
        ]
        result = augment_prompt_with_context("Q?", chunks, max_context_length=80)
        assert "[Context 1]" in result
