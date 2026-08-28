"""
The agent-to-knowledge-base relationship stays 1:1 during this migration.

Requirements 6.7, 6.8. This feature does *not* change cardinality: one agent, one
knowledge base, ``App_KB_Id == assistant_id``. That constraint is what lets the
access check be as simple as it is ("can this user invoke this agent" answers "may
this turn retrieve"), and what lets the data model key a knowledge base off the
assistant id with no join.

The rejections that enforce it already exist — ``binding_validation`` refuses an
explicit ``knowledge_base`` binding and ``bindable_catalog`` serves an empty
palette for the kind. Nothing in this feature touches either. That is exactly why
they are tested here: an untested constraint that some *other* feature relies on
is a constraint that gets relaxed by a plausible-looking change, and the symptom
would surface in this feature's code rather than in the change that caused it.
0..N bindings are F4's problem, and F4 is a separate spec (evaluation §10.6
forbids coupling the two).

Feature: managed-kb-migration
Requirements: 6.7, 6.8
"""

import pytest

from apis.app_api.agent_designer.services.bindable_catalog import BINDABLE_KINDS, list_bindable
from apis.app_api.agent_designer.services.binding_validation import (
    BindingValidationError,
    validate_agent_write,
)
from apis.shared.assistants.models import AgentBinding
from apis.shared.auth.models import User


def _user() -> User:
    return User(
        user_id="user-binding-freeze",
        email="author@example.test",
        name="Binding Freeze Author",
        roles=["default"],
    )


class TestAnExplicitKnowledgeBaseBindingIsRejected:
    @pytest.mark.asyncio
    async def test_a_knowledge_base_binding_is_refused(self):
        """Requirement 6.8. The Designer never offers this, so reaching it means
        a hand-rolled request — which is precisely the path that must not open a
        second knowledge base onto an agent."""
        with pytest.raises(BindingValidationError) as exc:
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="knowledge_base", ref="ast-somebody-else")],
            )

        assert exc.value.status_code == 400
        assert "knowledge_base" in str(exc.value)

    @pytest.mark.asyncio
    async def test_it_is_refused_even_when_the_ref_is_the_agents_own(self):
        """The self-referential case is the tempting one to allow: it looks like a
        no-op. It is not — it would make the binding author-settable, and the
        moment it is settable the cardinality is no longer enforced by the
        absence of a mechanism."""
        with pytest.raises(BindingValidationError):
            await validate_agent_write(
                _user(),
                bindings=[AgentBinding(kind="knowledge_base", ref="ast-self")],
            )

    @pytest.mark.asyncio
    async def test_an_empty_binding_list_is_accepted(self):
        """Sanity: the rejection above is about the kind, not about validation
        refusing everything — otherwise both tests would pass with the
        knowledge_base branch deleted."""
        await validate_agent_write(_user(), bindings=[])


class TestTheBindablePaletteOffersNoKnowledgeBases:
    @pytest.mark.asyncio
    async def test_the_knowledge_base_palette_is_empty(self):
        """Requirement 6.8. Welded to the agent, synthesized on read."""
        assert await list_bindable("knowledge_base", _user()) == []

    def test_the_kind_is_still_a_known_kind(self):
        """Empty because it is welded, **not** because it is unrecognized. If the
        kind were simply removed, this test would pass while
        ``compat.effective_bindings`` — which synthesizes a ``knowledge_base``
        binding on read — kept emitting a kind nothing recognized.
        """
        assert "knowledge_base" in BINDABLE_KINDS

    @pytest.mark.asyncio
    async def test_an_unknown_kind_still_raises(self):
        """So "returns an empty list" cannot be mistaken for "swallows anything"."""
        with pytest.raises(ValueError):
            await list_bindable("knowledge_bases", _user())


class TestTheSynthesizedBindingStaysOneToOne:
    def test_the_synthesized_binding_refs_the_assistant_itself(self):
        """``App_KB_Id == assistant_id`` (Requirement 6.5), read off the one
        function that produces the binding. When F4 lands, ``ref`` becomes a real
        knowledge base id with no shape change — and this test is what will say
        so out loud."""
        from apis.shared.assistants.compat import effective_bindings
        from apis.shared.assistants.models import Assistant

        assistant = Assistant.model_validate(
            {
                "assistantId": "ast-one-to-one",
                "ownerId": "user-binding-freeze",
                "ownerName": "Binding Freeze Author",
                "name": "One to one",
                "description": "",
                "instructions": "",
                "vectorIndexId": "idx-1",
                "visibility": "PRIVATE",
                "createdAt": "2026-07-01T00:00:00Z",
                "updatedAt": "2026-07-01T00:00:00Z",
                "status": "COMPLETE",
            }
        )

        kb_bindings = [b for b in effective_bindings(assistant) if b.kind == "knowledge_base"]

        assert len(kb_bindings) == 1, (
            "an agent must synthesize exactly one knowledge base binding; more "
            "than one is the 0..N model this phase deliberately does not build"
        )
        assert kb_bindings[0].ref == assistant.assistant_id
