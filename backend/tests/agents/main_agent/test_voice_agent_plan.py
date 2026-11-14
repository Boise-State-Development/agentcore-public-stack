"""Voice mode runs the conversation's Agent (#625).

Covers which Agent a voice session runs (the session's saved Agent wins over
the browser's claim), the resolution through the text path's resolver, the
voice-only refusals, and what the plan hands ``VoiceAgent``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.inference_api.chat import voice_agent_plan as vap
from apis.shared.auth.models import User


def _meta(assistant_id=None, message_count=0):
    prefs = SimpleNamespace(assistant_id=assistant_id) if assistant_id is not None else None
    return SimpleNamespace(preferences=prefs, message_count=message_count)


class TestCleanAgentClaim:
    def test_strips_and_accepts_a_string(self):
        assert vap.clean_agent_claim("  ast-1 ") == "ast-1"

    @pytest.mark.parametrize("value", [None, "", "   ", 42, {"id": "x"}, "x" * 129])
    def test_rejects_anything_that_is_not_an_agent_id(self, value):
        assert vap.clean_agent_claim(value) is None


class TestSessionAgentId:
    def test_a_new_session_takes_the_claim(self):
        assert vap.session_agent_id(None, "ast-1") == "ast-1"

    def test_a_new_session_without_a_claim_is_a_plain_chat(self):
        assert vap.session_agent_id(None, None) is None

    def test_the_saved_agent_wins_over_a_different_claim(self):
        assert vap.session_agent_id(_meta("ast-saved", 4), "ast-other") == "ast-saved"

    def test_the_saved_agent_applies_without_a_claim(self):
        # The browser cannot shed the Agent by leaving the id out.
        assert vap.session_agent_id(_meta("ast-saved", 4), None) == "ast-saved"

    def test_an_unbound_empty_conversation_takes_the_claim(self):
        assert vap.session_agent_id(_meta(None, 0), "ast-1") == "ast-1"

    def test_an_unbound_conversation_with_messages_ignores_the_claim(self):
        # The history was produced without the Agent's instructions.
        assert vap.session_agent_id(_meta(None, 3), "ast-1") is None


def _user():
    return User(email="a@example.edu", name="", user_id="u1", roles=["student"])


def _resolved(**overrides):
    assistant = SimpleNamespace(voice_enabled=True, voice_instructions_only=True)
    values = dict(
        assistant=assistant,
        project_id=None,
        kb_full_context_task=None,
        project_files_note_task=None,
        project_memory_task=None,
        tools_override=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def _done(value):
    return value


class TestResolveVoiceAgent:
    @pytest.mark.asyncio
    async def test_runs_the_agent_through_the_text_resolver_with_the_voice_budget(self, monkeypatch):
        from apis.inference_api.chat import routes

        monkeypatch.setenv("VOICE_KB_MAX_TOKENS", "123456")
        kb_task = asyncio.ensure_future(_done("<knowledge_base>vocab</knowledge_base>"))
        tools = SimpleNamespace(tool_ids=["calculator", "gateway_x::lookup"], owns_toolset=True)
        resolve = AsyncMock(return_value=_resolved(kb_full_context_task=kb_task, tools_override=tools))
        compose = MagicMock(return_value="BASE + agent instructions")

        with patch.object(routes, "resolve_turn_agent", resolve), patch.object(
            routes, "compose_turn_system_prompt", compose
        ):
            plan = await vap.resolve_voice_agent("sess-1", _user(), "ast-1")

        request = resolve.await_args.args[0]
        assert request.session_id == "sess-1"
        assert request.rag_assistant_id == "ast-1"
        assert resolve.await_args.kwargs["kb_budget_tokens"] == 123456
        assert compose.call_args.kwargs["personal_instructions"] is None
        assert plan.assistant_id == "ast-1"
        assert plan.system_prompt == "BASE + agent instructions"
        assert plan.knowledge_context == "<knowledge_base>vocab</knowledge_base>"
        assert plan.voice_guidelines is False
        assert plan.tool_ids == ["calculator", "gateway_x::lookup"]
        assert plan.owns_toolset is True

    @pytest.mark.asyncio
    async def test_the_author_can_turn_the_voice_guidelines_back_on(self):
        from apis.inference_api.chat import routes

        assistant = SimpleNamespace(voice_enabled=True, voice_instructions_only=False)
        with patch.object(routes, "resolve_turn_agent", AsyncMock(return_value=_resolved(assistant=assistant))), \
                patch.object(routes, "compose_turn_system_prompt", MagicMock(return_value="p")):
            plan = await vap.resolve_voice_agent("sess-1", _user(), "ast-1")
        assert plan.voice_guidelines is True
        assert plan.tool_ids is None  # Agent surface off: the request's tools apply

    @pytest.mark.asyncio
    async def test_an_agent_with_voice_off_is_refused(self):
        from apis.inference_api.chat import routes

        kb_task = asyncio.ensure_future(asyncio.sleep(10))
        assistant = SimpleNamespace(voice_enabled=False, voice_instructions_only=True)
        with patch.object(
            routes, "resolve_turn_agent",
            AsyncMock(return_value=_resolved(assistant=assistant, kb_full_context_task=kb_task)),
        ), patch.object(routes, "compose_turn_system_prompt", MagicMock()) as compose:
            with pytest.raises(vap.VoiceAgentRefused) as refused:
                await vap.resolve_voice_agent("sess-1", _user(), "ast-1")
        assert refused.value.message == vap.AGENT_VOICE_OFF_MESSAGE
        compose.assert_not_called()
        await asyncio.sleep(0)
        assert kb_task.cancelled()

    @pytest.mark.asyncio
    async def test_a_project_harness_is_refused(self):
        from apis.inference_api.chat import routes

        with patch.object(routes, "resolve_turn_agent", AsyncMock(return_value=_resolved(project_id="proj-1"))):
            with pytest.raises(vap.VoiceAgentRefused) as refused:
                await vap.resolve_voice_agent("sess-1", _user(), "ast-1")
        assert refused.value.message == vap.PROJECT_VOICE_MESSAGE

    @pytest.mark.asyncio
    async def test_the_text_paths_refusal_is_refused_without_streaming_it(self):
        """No access, deleted, blocked binding: the conversational refusal is never
        iterated, so nothing is written to the conversation."""
        from fastapi.responses import StreamingResponse

        from apis.inference_api.chat import routes

        iterated = []

        async def body():
            iterated.append(True)
            yield b"data: refused\n\n"

        with patch.object(routes, "resolve_turn_agent", AsyncMock(return_value=StreamingResponse(body()))):
            with pytest.raises(vap.VoiceAgentRefused) as refused:
                await vap.resolve_voice_agent("sess-1", _user(), "ast-1")
        assert refused.value.message == vap.AGENT_VOICE_REFUSED_MESSAGE
        assert iterated == []

    @pytest.mark.asyncio
    async def test_an_unavailable_published_version_is_refused(self):
        from fastapi import HTTPException

        from apis.inference_api.chat import routes

        with patch.object(routes, "resolve_turn_agent", AsyncMock(side_effect=HTTPException(status_code=503))):
            with pytest.raises(vap.VoiceAgentRefused):
                await vap.resolve_voice_agent("sess-1", _user(), "ast-1")
