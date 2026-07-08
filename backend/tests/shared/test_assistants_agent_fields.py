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
            bindings=[AgentBinding(kind="memory_space", ref="spc_1", config={"access": "readwrite"})],
        )
        got = await get_assistant(created.assistant_id, "u1")
        assert got is not None
        # Float survived the Decimal round trip as a native float.
        assert got.model_settings.model_id == "m1"
        assert got.model_settings.params["temperature"] == 0.7
        assert isinstance(got.model_settings.params["temperature"], float)
        assert got.model_settings.params["maxTokens"] == 4096
        assert len(got.bindings) == 1
        assert got.bindings[0].kind == "memory_space"
        assert got.bindings[0].config == {"access": "readwrite"}

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
