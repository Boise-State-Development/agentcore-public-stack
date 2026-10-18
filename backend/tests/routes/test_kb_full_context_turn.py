"""Full-context mode on the turn path.

The knowledge base block goes into the system prompt (ahead of the system cache
point, never into the paused-turn snapshot), the user message is left
unaugmented, and the agent cache key tells a full-context agent from a
retrieval one. The loader itself is covered by ``tests/shared/test_kb_full_context.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import apis.inference_api.chat.routes as routes
from agents.main_agent.chat_agent import ChatAgent
from apis.inference_api.chat import service
from apis.shared.assistants import rag_service
from apis.shared.assistants.kb_access import granted

ASSISTANT_ID = "ast-course-0001"
ACCESS = granted(ASSISTANT_ID, "user-1", "owner")
BLOCK = "<knowledge_base>\n\nSyllabus: AI policy varies by assignment.\n\n</knowledge_base>"
CHUNKS = [{"text": "Video Post: AI use is neutral.", "distance": 0.1, "metadata": {"document_id": "d1"}, "key": "d1#0"}]


async def _done(value):
    return value


class TestSearchAndAugment:
    @pytest.mark.asyncio
    async def test_a_full_context_turn_keeps_the_users_words_and_the_citations(self):
        async def _search(**kwargs):
            return list(CHUNKS), 22000

        with patch.object(rag_service, "search_assistant_knowledgebase_with_cap", side_effect=_search):
            chunks, message = await routes._search_and_augment(
                assistant_id=ASSISTANT_ID,
                message="Can I use AI on my assignment?",
                access=ACCESS,
                full_context=asyncio.ensure_future(_done(BLOCK)),
            )

        assert chunks == CHUNKS  # the citation cards still come from retrieval
        assert message == "Can I use AI on my assignment?"

    @pytest.mark.asyncio
    async def test_an_ineligible_knowledge_base_retrieves_as_before(self):
        async def _search(**kwargs):
            return list(CHUNKS), 22000

        with patch.object(rag_service, "search_assistant_knowledgebase_with_cap", side_effect=_search):
            chunks, message = await routes._search_and_augment(
                assistant_id=ASSISTANT_ID,
                message="Can I use AI on my assignment?",
                access=ACCESS,
                full_context=asyncio.ensure_future(_done(None)),
            )

        assert message == rag_service.augment_prompt_with_context(
            user_message="Can I use AI on my assignment?", context_chunks=CHUNKS, max_context_length=22000
        )


class TestResume:
    @pytest.mark.asyncio
    async def test_a_resume_reloads_the_block_under_a_fresh_grant(self, monkeypatch):
        monkeypatch.setenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", "true")
        user = SimpleNamespace(email="i@example.edu")
        with patch("apis.shared.assistants.kb_access.resolve_kb_access", new=AsyncMock(return_value=ACCESS)) as resolve, \
             patch("apis.shared.assistants.kb_full_context.load_full_kb_context", new=AsyncMock(return_value=BLOCK)) as load:
            assert await routes._resume_knowledge_context(ASSISTANT_ID, user, "user-1") == BLOCK

        resolve.assert_awaited_once_with(ASSISTANT_ID, "user-1", user_email="i@example.edu")
        assert load.await_args.kwargs["access"] is ACCESS

    @pytest.mark.asyncio
    async def test_a_resume_without_an_assistant_or_with_the_flag_off_loads_nothing(self, monkeypatch):
        user = SimpleNamespace(email="i@example.edu")
        with patch("apis.shared.assistants.kb_access.resolve_kb_access", new=AsyncMock(side_effect=AssertionError)):
            assert await routes._resume_knowledge_context(None, user, "user-1") is None
            monkeypatch.delenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", raising=False)
            assert await routes._resume_knowledge_context(ASSISTANT_ID, user, "user-1") is None


class TestSystemPrompt:
    def _agent(self, knowledge=None) -> ChatAgent:
        agent = ChatAgent.__new__(ChatAgent)
        agent.system_prompt = "BASE PROMPT"
        if knowledge is not None:
            agent.knowledge_context = knowledge
        return agent

    def test_the_block_ends_the_system_prompt(self):
        assert self._agent(BLOCK)._system_prompt_for([]) == f"BASE PROMPT\n\n{BLOCK}"

    def test_absent_without_full_context(self):
        assert self._agent()._system_prompt_for([]) == "BASE PROMPT"
        assert self._agent("")._system_prompt_for([]) == "BASE PROMPT"

    def test_the_block_is_not_on_the_resume_snapshot(self):
        """The snapshot is written into the session's metadata row; ~200 KB there
        would crowd the 400 KB item limit. Resume re-loads the block instead."""
        from agents.main_agent import base_agent as ba

        class Probe(ba.BaseAgent):
            def _create_agent(self):
                pass

            async def stream_async(self, *a, **k):  # pragma: no cover - abstract stub
                yield None

        with patch.object(ba, "create_default_registry", return_value=MagicMock()), \
             patch.object(ba, "ToolFilter", return_value=MagicMock()), \
             patch.object(ba.BaseAgent, "_register_external_mcp_tools", lambda self: None), \
             patch.object(ba, "GatewayIntegration", return_value=MagicMock()), \
             patch.object(ba, "SessionFactory"), \
             patch.object(ba, "StreamCoordinator", return_value=MagicMock()):
            agent = Probe(session_id="s", user_id="u", system_prompt="BASE PROMPT", knowledge_context=BLOCK)

        assert agent.knowledge_context == BLOCK
        assert BLOCK not in repr(agent._construction_snapshot)


class TestCacheKey:
    def _key(self, **kwargs):
        base = dict(
            session_id="s", user_id="u", enabled_tools=None, model_id="m", inference_params=None,
            system_prompt="BASE PROMPT", caching_enabled=True, provider="bedrock", freshness_hash="f",
            agent_type="chat", assistant_id=ASSISTANT_ID,
        )
        base.update(kwargs)
        return service._create_cache_key(**base)

    def test_a_full_context_agent_and_a_retrieval_agent_never_share_a_slot(self):
        assert self._key(knowledge_context=BLOCK) != self._key()

    def test_the_same_block_hits_the_same_slot(self):
        assert self._key(knowledge_context=BLOCK) == self._key(knowledge_context="".join(list(BLOCK)))

    def test_a_changed_knowledge_base_misses(self):
        assert self._key(knowledge_context=BLOCK) != self._key(knowledge_context=BLOCK + " ")

    def test_keys_without_it_are_unchanged(self):
        assert self._key(knowledge_context=None) == self._key()

    @pytest.mark.asyncio
    async def test_get_agent_hands_the_block_to_the_agent(self):
        service.clear_agent_cache()
        with patch.object(service, "create_agent") as create, patch(
            "apis.shared.tools.freshness.get_freshness_hash", new=AsyncMock(return_value="fresh")
        ), patch("apis.shared.tools.freshness.get_disabled_tool_ids", new=AsyncMock(return_value=frozenset())):
            create.side_effect = lambda **kwargs: MagicMock(spec=["agent", "_construction_snapshot"])
            await service.get_agent(session_id="s", user_id="u", assistant_id=ASSISTANT_ID, knowledge_context=BLOCK)
            await service.get_agent(session_id="s2", user_id="u")
        service.clear_agent_cache()

        full, plain = create.call_args_list
        assert full.kwargs["knowledge_context"] == BLOCK
        assert "knowledge_context" not in plain.kwargs
