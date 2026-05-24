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
        assert await check_share_access(created.assistant_id, "bob@example.com") == "viewer"
        assert await check_share_access(created.assistant_id, "eve@example.com") is None

    @pytest.mark.asyncio
    async def test_unshare(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, share_assistant, unshare_assistant, check_share_access
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"])
        await unshare_assistant(created.assistant_id, "u1", ["bob@example.com"])
        assert await check_share_access(created.assistant_id, "bob@example.com") is None

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


class TestSharePermissions:
    """Issue #113 — viewer/editor permission levels on shares."""

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_share_with_explicit_viewer_permission(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            check_share_access,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="viewer"
        ) is True
        assert await check_share_access(created.assistant_id, "bob@example.com") == "viewer"

    @pytest.mark.asyncio
    async def test_share_with_editor_permission(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            check_share_access,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="editor"
        ) is True
        assert await check_share_access(created.assistant_id, "bob@example.com") == "editor"

    @pytest.mark.asyncio
    async def test_share_rejects_invalid_permission(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, share_assistant
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="admin"
        ) is False

    @pytest.mark.asyncio
    async def test_legacy_share_without_permission_defaults_to_viewer(self, assistants_table):
        """Existing share records that pre-date this feature lack the `permission`
        attribute. Reads must default them to viewer for backward compatibility.
        """
        import boto3
        from apis.shared.assistants.service import create_assistant, check_share_access

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )

        # Write a legacy-shaped share record directly (no `permission` attribute)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.Table("test-assistants").put_item(
            Item={
                "PK": f"AST#{created.assistant_id}",
                "SK": "SHARE#legacy@example.com",
                "GSI3_PK": "SHARE#legacy@example.com",
                "GSI3_SK": f"AST#{created.assistant_id}",
                "assistantId": created.assistant_id,
                "email": "legacy@example.com",
                "createdAt": "2024-01-01T00:00:00Z",
                "firstInteracted": False,
            }
        )

        assert await check_share_access(created.assistant_id, "legacy@example.com") == "viewer"

    @pytest.mark.asyncio
    async def test_resolve_assistant_permission_owner(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            resolve_assistant_permission,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assistant, permission = await resolve_assistant_permission(
            created.assistant_id, "u1", "alice@test.com"
        )
        assert assistant is not None
        assert permission == "owner"

    @pytest.mark.asyncio
    async def test_resolve_assistant_permission_editor(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            resolve_assistant_permission,
            share_assistant,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="editor"
        )
        assistant, permission = await resolve_assistant_permission(
            created.assistant_id, "u2", "bob@example.com"
        )
        assert assistant is not None
        assert permission == "editor"

    @pytest.mark.asyncio
    async def test_resolve_assistant_permission_viewer(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            resolve_assistant_permission,
            share_assistant,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="viewer"
        )
        assistant, permission = await resolve_assistant_permission(
            created.assistant_id, "u2", "bob@example.com"
        )
        assert assistant is not None
        assert permission == "viewer"

    @pytest.mark.asyncio
    async def test_resolve_assistant_permission_no_share(self, assistants_table):
        """Non-owner with no share record resolves to (assistant, None) — caller
        gates this as a 403. Returning the assistant lets the caller distinguish
        no-permission from not-found without a second DB read.
        """
        from apis.shared.assistants.service import (
            create_assistant,
            resolve_assistant_permission,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assistant, permission = await resolve_assistant_permission(
            created.assistant_id, "u2", "eve@example.com"
        )
        assert assistant is not None
        assert permission is None

    @pytest.mark.asyncio
    async def test_resolve_assistant_permission_not_found(self, assistants_table):
        from apis.shared.assistants.service import resolve_assistant_permission
        assistant, permission = await resolve_assistant_permission(
            "ast-nope", "u1", "alice@example.com"
        )
        assert assistant is None
        assert permission is None

    @pytest.mark.asyncio
    async def test_update_share_permission_upgrades_viewer_to_editor(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            check_share_access,
            update_share_permission,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"], permission="viewer")
        assert await check_share_access(created.assistant_id, "bob@example.com") == "viewer"

        assert await update_share_permission(
            created.assistant_id, "u1", "bob@example.com", "editor"
        ) is True
        assert await check_share_access(created.assistant_id, "bob@example.com") == "editor"

    @pytest.mark.asyncio
    async def test_update_share_permission_requires_owner(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            update_share_permission,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"], permission="viewer")
        # u2 is not the owner — update should fail
        assert await update_share_permission(
            created.assistant_id, "u2", "bob@example.com", "editor"
        ) is False

    @pytest.mark.asyncio
    async def test_update_share_permission_missing_share(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, update_share_permission
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        assert await update_share_permission(
            created.assistant_id, "u1", "noone@example.com", "editor"
        ) is False

    @pytest.mark.asyncio
    async def test_update_share_permission_rejects_invalid(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            update_share_permission,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"])
        assert await update_share_permission(
            created.assistant_id, "u1", "bob@example.com", "admin"
        ) is False

    @pytest.mark.asyncio
    async def test_list_assistant_shares_returns_email_and_permission(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            list_assistant_shares,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(created.assistant_id, "u1", ["bob@example.com"], permission="editor")
        await share_assistant(created.assistant_id, "u1", ["carol@example.com"], permission="viewer")

        shares = await list_assistant_shares(created.assistant_id, "u1")
        by_email = {s["email"]: s["permission"] for s in shares}
        assert by_email == {"bob@example.com": "editor", "carol@example.com": "viewer"}

    @pytest.mark.asyncio
    async def test_list_shared_with_user_surfaces_permission(self, assistants_table):
        from apis.shared.assistants.service import (
            create_assistant,
            share_assistant,
            list_shared_with_user,
        )
        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot",
            description="d", instructions="hi",
        )
        await share_assistant(
            created.assistant_id, "u1", ["bob@example.com"], permission="editor"
        )
        shared = await list_shared_with_user("bob@example.com")
        assert len(shared) == 1
        assert getattr(shared[0], "user_permission", None) == "editor"


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
