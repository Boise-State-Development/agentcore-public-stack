"""Agent Designer Phase 1 — compat mapping, serialization, and model-contract tests.

Pure library tests (no boto3): the D2 read-side compat mapping, the D3 model/binding
shapes, the R3 pydantic naming landmine, and the Decimal round-trip helpers.
"""

from decimal import Decimal

from apis.shared.assistants.compat import effective_bindings, to_agent_view
from apis.shared.assistants.models import AgentBinding, AgentModelConfig, Assistant
from apis.shared.assistants.serialization import from_ddb, to_ddb_safe


def _legacy_assistant(**overrides) -> Assistant:
    """A legacy Assistant row — no bindings, no modelConfig."""
    base = dict(
        assistant_id="ast_123",
        owner_id="u1",
        owner_name="Alice",
        name="Bot",
        description="A bot",
        instructions="You are helpful.",
        vector_index_id="assistants-index",
        visibility="PRIVATE",
        created_at="2026-07-07T00:00:00Z",
        updated_at="2026-07-07T00:00:00Z",
        status="COMPLETE",
    )
    base.update(overrides)
    return Assistant(**base)


class TestCompatMapping:
    def test_legacy_synthesizes_single_kb_binding_reffing_assistant_id(self):
        a = _legacy_assistant()
        bindings = effective_bindings(a)
        assert len(bindings) == 1
        (kb,) = bindings
        assert kb.kind == "knowledge_base"
        # The KB's only stable identity today IS the assistant id (R4).
        assert kb.ref == "ast_123"
        assert kb.config == {"vectorIndexId": "assistants-index"}

    def test_legacy_modelconfig_is_none_not_fabricated(self):
        # R1: absent model must map to None ("resolve as today"), never a fake id.
        a = _legacy_assistant()
        assert a.model_settings is None
        assert to_agent_view(a)["modelConfig"] is None

    def test_stored_bindings_returned_verbatim(self):
        stored = [AgentBinding(kind="memory_space", ref="spc_1", config={"access": "readwrite"})]
        a = _legacy_assistant(bindings=stored)
        assert effective_bindings(a) == stored

    def test_unknown_kind_survives_read(self):
        # Forward/rollback compat: a kind written by newer code passes through.
        a = _legacy_assistant(bindings=[AgentBinding(kind="future_kind", ref="x", config={})])
        assert effective_bindings(a)[0].kind == "future_kind"

    def test_empty_bindings_list_is_not_synthesized(self):
        # An explicit empty list means "no bindings", distinct from absent (legacy).
        a = _legacy_assistant(bindings=[])
        assert effective_bindings(a) == []

    def test_agent_view_uses_agent_id_and_omits_owner_id(self):
        view = to_agent_view(_legacy_assistant())
        assert view["agentId"] == "ast_123"
        assert "ownerId" not in view and "owner_id" not in view

    def test_agent_view_projects_the_listing_block(self):
        """Marketplace Phase 3: the detail read carries ``listing``, so the projection
        has to actually emit it — the field existed on the response model from Phase 1
        but nothing populated it."""
        from apis.shared.assistants.models import AgentListing

        a = _legacy_assistant(
            tagline="Find and cite university policy",
            listing=AgentListing(
                state="published", category="Administration", publisher_id="pub-registrar"
            ),
        )
        view = to_agent_view(a)

        assert view["tagline"] == "Find and cite university policy"
        assert view["listing"]["state"] == "published"
        assert view["listing"]["publisherId"] == "pub-registrar"

    def test_an_unsubmitted_agents_marketplace_fields_are_all_none(self):
        """``response_model_exclude_none`` then drops them, so the payload for an agent
        that was never submitted is byte-identical to before the marketplace shipped."""
        view = to_agent_view(_legacy_assistant())

        assert view["listing"] is None
        assert view["tagline"] is None
        assert view["iconKey"] is None


class TestModelContract:
    def test_modelconfig_alias_roundtrip(self):
        # R3: field is ``model_settings`` in Python, ``modelConfig`` on the wire.
        a = _legacy_assistant(
            model_settings=AgentModelConfig(model_id="us.anthropic.claude", params={"temperature": 0.7})
        )
        assert a.model_settings.model_id == "us.anthropic.claude"
        dumped = a.model_dump(by_alias=True)
        assert dumped["modelConfig"]["modelId"] == "us.anthropic.claude"

    def test_assistant_validates_modelconfig_from_wire_alias(self):
        a = Assistant.model_validate(
            {
                "assistantId": "ast_9",
                "ownerId": "u1",
                "ownerName": "Alice",
                "name": "B",
                "description": "d",
                "instructions": "i",
                "vectorIndexId": "assistants-index",
                "visibility": "PRIVATE",
                "createdAt": "t",
                "updatedAt": "t",
                "status": "COMPLETE",
                "modelConfig": {"modelId": "m1"},
                "bindings": [{"kind": "tool", "ref": "t1", "config": {}}],
            }
        )
        assert a.model_settings.model_id == "m1"
        assert a.bindings[0].kind == "tool"

    def test_binding_config_defaults_to_empty_dict(self):
        assert AgentBinding(kind="skill", ref="s1").config == {}


class TestSerialization:
    def test_float_to_decimal_and_back(self):
        params = {"temperature": 0.7, "topP": 1.0, "maxTokens": 4096, "stop": ["x"], "stream": True}
        safe = to_ddb_safe(params)
        assert isinstance(safe["temperature"], Decimal)
        # Floats that happen to be integral still convert (they arrived as float).
        assert isinstance(safe["topP"], Decimal)
        # Native ints are already DynamoDB-safe — left untouched.
        assert isinstance(safe["maxTokens"], int)
        # bool must not be coerced to Decimal.
        assert safe["stream"] is True
        back = from_ddb(safe)
        assert back["temperature"] == 0.7 and isinstance(back["temperature"], float)
        # Integral decimals read back as int, not 1.0.
        assert back["topP"] == 1 and isinstance(back["topP"], int)
        assert back["maxTokens"] == 4096 and isinstance(back["maxTokens"], int)
        assert back["stop"] == ["x"]
