"""Agent Designer Phase 1 — bindings + modelConfig persistence round-trip (moto).

Proves the D3 fields survive a real DynamoDB write/read: float params round-trip through
Decimal, bindings are preserved, and legacy rows still read back with no agent fields.
"""

import pytest

from apis.shared.assistants.models import AgentBinding, AgentModelConfig


class TestAgentFieldsPersistence:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_create_with_bindings_and_modelconfig_roundtrips(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1",
            owner_name="Alice",
            name="Oliver",
            description="Chief of Staff",
            instructions="You are Oliver.",
            model_settings=AgentModelConfig(model_id="m1", params={"temperature": 0.7, "maxTokens": 4096}),
            bindings=[AgentBinding(kind="skill", ref="sk_1", config={"pinned": True})],
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got is not None
        # Float survived the Decimal round trip as a native float.
        assert got.model_settings.model_id == "m1"
        assert got.model_settings.params["temperature"] == 0.7
        assert isinstance(got.model_settings.params["temperature"], float)
        assert got.model_settings.params["maxTokens"] == 4096
        assert len(got.bindings) == 1
        assert got.bindings[0].kind == "skill"
        assert got.bindings[0].config == {"pinned": True}

    @pytest.mark.asyncio
    async def test_legacy_create_has_no_agent_fields(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Plain", description="d", instructions="i"
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.model_settings is None
        assert got.bindings is None  # absent → compat synthesizes KB on read

    @pytest.mark.asyncio
    async def test_update_sets_bindings(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant, update_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i"
        )
        await update_assistant(
            assistant_id=created.assistant_id,
            owner_id="u1",
            bindings=[AgentBinding(kind="tool", ref="gateway_x", config={})],
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.bindings is not None and got.bindings[0].kind == "tool"


class TestCitationDownloadFlags:
    """Issue #111 — showCitations / allowDocumentDownload persistence + defaults."""

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_legacy_create_defaults_both_flags_true(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Plain", description="d", instructions="i"
        )
        got = await get_assistant(created.assistant_id, "u1")
        # No migration: an agent created without the flags behaves exactly as today.
        assert got.show_citations is True
        assert got.allow_document_download is True

    @pytest.mark.asyncio
    async def test_create_opt_out_roundtrips(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Private", description="d", instructions="i",
            show_citations=False, allow_document_download=False,
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.show_citations is False
        assert got.allow_document_download is False

    @pytest.mark.asyncio
    async def test_update_can_toggle_and_none_leaves_unchanged(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant, update_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i"
        )
        # Opt out of downloads only; show_citations left None → unchanged (stays True).
        await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1", allow_document_download=False
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.show_citations is True
        assert got.allow_document_download is False

        # Now turn citations off; downloads value untouched by this call.
        await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1", show_citations=False
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.show_citations is False
        assert got.allow_document_download is False

    @pytest.mark.asyncio
    async def test_agent_view_projects_flags(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant
        from apis.shared.assistants.compat import to_agent_view

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i",
            show_citations=True, allow_document_download=False,
        )
        got = await get_assistant(created.assistant_id, "u1")
        view = to_agent_view(got)
        assert view["showCitations"] is True
        assert view["allowDocumentDownload"] is False


class TestResponseStrictness:
    """Issue #112 — responseStrictness persistence + defaults."""

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_default_is_balanced(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Plain", description="d", instructions="i"
        )
        assert (await get_assistant(created.assistant_id, "u1")).response_strictness == "balanced"

    @pytest.mark.asyncio
    async def test_create_roundtrips_and_view_projects_it(self, assistants_table):
        from apis.shared.assistants.compat import to_agent_view
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i",
            response_strictness="strict",
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.response_strictness == "strict"
        assert to_agent_view(got)["responseStrictness"] == "strict"

    @pytest.mark.asyncio
    async def test_update_none_leaves_unchanged_and_value_changes_it(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant, update_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i",
            response_strictness="strict",
        )
        await update_assistant(assistant_id=created.assistant_id, owner_id="u1", name="Renamed")
        assert (await get_assistant(created.assistant_id, "u1")).response_strictness == "strict"

        await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1", response_strictness="flexible"
        )
        assert (await get_assistant(created.assistant_id, "u1")).response_strictness == "flexible"

    def test_request_models_reject_an_invalid_value(self):
        from pydantic import ValidationError

        from apis.shared.assistants.models import CreateAssistantRequest, UpdateAssistantRequest

        with pytest.raises(ValidationError):
            CreateAssistantRequest(
                name="n", description="d", instructions="i", responseStrictness="loose"
            )
        with pytest.raises(ValidationError):
            UpdateAssistantRequest(responseStrictness="loose")


class TestVoiceSettings:
    """Issue #625 — voiceEnabled / voiceInstructionsOnly persistence + defaults."""

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")

    @pytest.mark.asyncio
    async def test_voice_is_off_and_instructions_only_by_default(self, assistants_table):
        from apis.shared.assistants.compat import to_agent_view
        from apis.shared.assistants.service import create_assistant, get_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Plain", description="d", instructions="i"
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.voice_enabled is False
        assert got.voice_instructions_only is True
        view = to_agent_view(got)
        assert view["voiceEnabled"] is False
        assert view["voiceInstructionsOnly"] is True

    @pytest.mark.asyncio
    async def test_update_none_leaves_unchanged_and_value_changes_it(self, assistants_table):
        from apis.shared.assistants.service import create_assistant, get_assistant, update_assistant

        created = await create_assistant(
            owner_id="u1", owner_name="Alice", name="Bot", description="d", instructions="i",
            voice_enabled=True,
        )
        await update_assistant(assistant_id=created.assistant_id, owner_id="u1", name="Renamed")
        got = await get_assistant(created.assistant_id, "u1")
        assert got.voice_enabled is True
        assert got.voice_instructions_only is True

        await update_assistant(
            assistant_id=created.assistant_id, owner_id="u1", voice_enabled=False, voice_instructions_only=False
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got.voice_enabled is False
        assert got.voice_instructions_only is False

    def test_a_legacy_row_reads_voice_off(self):
        from apis.shared.assistants.models import Assistant

        legacy = Assistant(
            assistantId="ast-1", ownerId="u1", ownerName="U", name="n", description="d",
            instructions="i", vectorIndexId="v", visibility="PRIVATE", status="COMPLETE",
            createdAt="2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z",
        )
        assert legacy.voice_enabled is False
        assert legacy.voice_instructions_only is True

    def test_update_request_leaves_unset_fields_none(self):
        from apis.shared.assistants.models import UpdateAssistantRequest

        req = UpdateAssistantRequest(name="n")
        assert req.voice_enabled is None
        assert req.voice_instructions_only is None
        assert UpdateAssistantRequest(voiceEnabled=True).voice_enabled is True
