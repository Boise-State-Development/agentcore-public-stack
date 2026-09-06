"""Tests for DisplayTextHook — persist the user's own words at append time.

The bug this exists to close: `displayText` used to be written only by the
stream coordinator's success path, so any turn that was stopped, dropped, or
errored left the *augmented* prompt as the only thing the UI could render.
That put a model-directed `<interruption_note>` in the user's own chat bubble,
permanently — and it landed most often on exactly the turns carrying such a
note, since the note only exists because the previous turn was interrupted.

So the properties under test, in order of how expensive they are to get wrong:

1. **The write happens on append, before the model call.** That is what makes
   it independent of how the turn ends.
2. **One-shot per turn.** Tool-result messages are role `user` too; a second
   write would relabel the wrong message index.
3. **No stale arm.** The agent instance is cached across turns (#741/#751), so
   an un-armed turn must never inherit the previous turn's text.
4. Fail-soft: a storage failure never propagates into the turn.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from strands.hooks import MessageAddedEvent

from agents.main_agent.session.hooks.display_text import DisplayTextHook


def _user_message(text: str = "hi"):
    return {"role": "user", "content": [{"text": text}]}


def _assistant_message():
    return {"role": "assistant", "content": [{"text": "answer"}]}


def _tool_result_message():
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}}],
    }


def _event(message):
    return MessageAddedEvent(agent=MagicMock(), message=message)


@pytest.fixture
def armed_hook():
    hook = DisplayTextHook()
    hook.arm(
        session_id="s1",
        user_id="u1",
        message_index=4,
        display_text="what the user actually typed",
    )
    return hook


@pytest.fixture
def store():
    with patch(
        "apis.shared.sessions.metadata.store_user_display_text", new_callable=AsyncMock
    ) as mock:
        yield mock


class TestWritesOnAppend:
    @pytest.mark.asyncio
    async def test_stores_the_original_text_when_the_user_message_lands(
        self, armed_hook, store
    ):
        await armed_hook.write_display_text(_event(_user_message()))

        store.assert_awaited_once_with(
            session_id="s1",
            user_id="u1",
            message_id=4,
            display_text="what the user actually typed",
        )
        assert armed_hook.wrote_this_turn is True

    @pytest.mark.asyncio
    async def test_ignores_assistant_messages(self, armed_hook, store):
        await armed_hook.write_display_text(_event(_assistant_message()))

        store.assert_not_awaited()
        # Still armed — the user turn hasn't landed yet.
        assert armed_hook.wrote_this_turn is False

    @pytest.mark.asyncio
    async def test_writes_once_even_though_tool_results_are_role_user(
        self, armed_hook, store
    ):
        """Under Bedrock Converse a tool-result message is role `user` too.

        A second write would stamp this turn's clean text onto a message index
        that isn't the user's prompt.
        """
        await armed_hook.write_display_text(_event(_user_message()))
        await armed_hook.write_display_text(_event(_tool_result_message()))
        await armed_hook.write_display_text(_event(_tool_result_message()))

        assert store.await_count == 1

    @pytest.mark.asyncio
    async def test_a_synthetic_tool_result_repair_does_not_consume_the_arm(
        self, armed_hook, store
    ):
        """Strands prepends a role-`user` tool-result message ahead of the
        prompt when history ends on a dangling `toolUse` (agent.py, "appending
        a toolResult message to have valid conversation").

        That is exactly the shape an interrupted tool turn leaves behind — the
        case this hook exists for — so consuming the arm there would stamp the
        clean text onto the repair message and leave the user's own prompt
        showing the augmented text.
        """
        await armed_hook.write_display_text(_event(_tool_result_message()))
        store.assert_not_awaited()

        await armed_hook.write_display_text(_event(_user_message()))

        store.assert_awaited_once_with(
            session_id="s1",
            user_id="u1",
            message_id=4,
            display_text="what the user actually typed",
        )

    @pytest.mark.asyncio
    async def test_a_tool_use_message_does_not_consume_the_arm(self, armed_hook, store):
        await armed_hook.write_display_text(
            _event(
                {
                    "role": "user",
                    "content": [{"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}],
                }
            )
        )

        store.assert_not_awaited()


class TestArming:
    @pytest.mark.asyncio
    async def test_unarmed_hook_writes_nothing(self, store):
        hook = DisplayTextHook()

        await hook.write_display_text(_event(_user_message()))

        store.assert_not_awaited()
        assert hook.wrote_this_turn is False

    @pytest.mark.asyncio
    async def test_arming_with_no_text_disarms(self, store):
        """A turn that sends the user's text verbatim — and every resume /
        continuation, which sends no new user turn at all — passes None."""
        hook = DisplayTextHook()
        hook.arm(session_id="s1", user_id="u1", message_index=4, display_text="orig")
        hook.arm(session_id="s1", user_id="u1", message_index=6, display_text=None)

        await hook.write_display_text(_event(_user_message()))

        store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_re_arming_replaces_the_previous_turns_state(self, store):
        """The agent instance is cached across turns (#741/#751).

        A second turn must write ITS text at ITS index, never the first's.
        """
        hook = DisplayTextHook()
        hook.arm(session_id="s1", user_id="u1", message_index=4, display_text="first")
        hook.arm(session_id="s1", user_id="u1", message_index=6, display_text="second")

        await hook.write_display_text(_event(_user_message()))

        store.assert_awaited_once_with(
            session_id="s1", user_id="u1", message_id=6, display_text="second"
        )

    @pytest.mark.asyncio
    async def test_re_arming_clears_the_written_flag(self, armed_hook, store):
        """`wrote_this_turn` gates the coordinator's backstop, so a stale True
        from last turn would suppress a write this turn genuinely needs."""
        await armed_hook.write_display_text(_event(_user_message()))
        assert armed_hook.wrote_this_turn is True

        armed_hook.arm(
            session_id="s1", user_id="u1", message_index=6, display_text="next turn"
        )

        assert armed_hook.wrote_this_turn is False


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_a_storage_failure_never_reaches_the_turn(self, armed_hook):
        with patch(
            "apis.shared.sessions.metadata.store_user_display_text",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dynamo down"),
        ):
            await armed_hook.write_display_text(_event(_user_message()))

        # Not marked written, so the coordinator's end-of-turn backstop still
        # runs for a turn that completes.
        assert armed_hook.wrote_this_turn is False

    @pytest.mark.asyncio
    async def test_a_message_without_a_role_is_ignored(self, armed_hook, store):
        await armed_hook.write_display_text(_event({"content": []}))

        store.assert_not_awaited()


class TestRegistration:
    def test_registers_for_message_added(self):
        hook = DisplayTextHook()
        registry = MagicMock()

        hook.register_hooks(registry)

        registered = {call.args[0] for call in registry.add_callback.call_args_list}
        assert MessageAddedEvent in registered
