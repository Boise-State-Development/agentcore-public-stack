"""Tests for SteeringHook — mid-turn steering injection at a tool boundary.

See docs/specs/mid-turn-steering.md. The properties under test, in order of
how expensive they are to get wrong:

1. **Commit-on-append.** ``AfterToolsEvent`` fires from a ``finally`` and so
   also fires on the interrupt path, where the mutated message is discarded.
   The hook must NOT consume the inbox on read, or a steer that lands on the
   same tool batch as an OAuth consent silently destroys the user's words.
2. **The SDK contract.** ``HookEvent.__setattr__`` is write-guarded; mutating
   the message dict in place is not blocked but is not sanctioned either. The
   contract test here is the canary for a ``strands-agents`` bump.
3. Fail-soft everywhere else: flag off, no lease, empty batch, failed read.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from strands.hooks import AfterToolsEvent, MessageAddedEvent

from agents.main_agent.session.hooks.steering import (
    STEER_CLOSE_TAG,
    STEER_OPEN_TAG,
    SteeringHook,
)
from apis.shared.sessions.session_lease import SessionLease


@pytest.fixture
def lease():
    return SessionLease(session_id="s1", user_id="u1", owner="owner-1")


@pytest.fixture
def session_manager(lease):
    manager = MagicMock()
    manager.turn_lease = lease
    return manager


def _tool_result_message(count: int = 1):
    return {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": f"t{i}", "content": [{"text": "ok"}]}}
            for i in range(count)
        ],
    }


def _after_tools(message):
    return AfterToolsEvent(agent=MagicMock(), message=message, invocation_state={})


def _patch_lease(monkeypatch, *, peek=None, clear=None):
    import apis.shared.sessions.session_lease as mod

    monkeypatch.setattr(mod, "peek_steer_queue", AsyncMock(return_value=peek or []))
    monkeypatch.setattr(mod, "clear_steer_entry", clear or AsyncMock(return_value=True))
    return mod


class TestInjection:
    @pytest.mark.asyncio
    async def test_appends_a_wrapped_text_block_to_the_tool_results(
        self, monkeypatch, session_manager
    ):
        _patch_lease(monkeypatch, peek=[{"id": "e1", "text": "use the other file"}])
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))

        assert len(message["content"]) == 2
        # The tool results are untouched and still lead the message: the
        # injection is append-only against the cached prefix.
        assert "toolResult" in message["content"][0]
        text = message["content"][1]["text"]
        assert text.startswith(STEER_OPEN_TAG)
        assert text.endswith(STEER_CLOSE_TAG)
        assert "use the other file" in text

    @pytest.mark.asyncio
    async def test_multiple_entries_ride_one_block_in_arrival_order(
        self, monkeypatch, session_manager
    ):
        _patch_lease(
            monkeypatch,
            peek=[{"id": "e1", "text": "first"}, {"id": "e2", "text": "second"}],
        )
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))

        text = message["content"][1]["text"]
        assert text.index("first") < text.index("second")

    @pytest.mark.asyncio
    async def test_noop_on_an_empty_tool_batch(self, monkeypatch, session_manager):
        peek = AsyncMock(return_value=[{"id": "e1", "text": "hi"}])
        import apis.shared.sessions.session_lease as mod

        monkeypatch.setattr(mod, "peek_steer_queue", peek)
        hook = SteeringHook(session_manager)
        # Cancelled before any tool ran: no message will be appended, so there
        # is nothing for the injection to ride.
        message = {"role": "user", "content": []}

        await hook.inject_pending_steering(_after_tools(message))

        assert message["content"] == []
        peek.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_without_a_turn_lease(self, monkeypatch):
        peek = AsyncMock(return_value=[{"id": "e1", "text": "hi"}])
        import apis.shared.sessions.session_lease as mod

        monkeypatch.setattr(mod, "peek_steer_queue", peek)
        manager = MagicMock()
        manager.turn_lease = None  # preview session / local dev
        hook = SteeringHook(manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))

        assert len(message["content"]) == 1
        peek.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_the_flag_is_off(self, monkeypatch, session_manager):
        monkeypatch.setenv("MID_TURN_STEERING_ENABLED", "false")
        peek = AsyncMock(return_value=[{"id": "e1", "text": "hi"}])
        import apis.shared.sessions.session_lease as mod

        monkeypatch.setattr(mod, "peek_steer_queue", peek)
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))

        assert len(message["content"]) == 1
        peek.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_read_leaves_the_message_untouched(
        self, monkeypatch, session_manager
    ):
        import apis.shared.sessions.session_lease as mod

        monkeypatch.setattr(
            mod, "peek_steer_queue", AsyncMock(side_effect=RuntimeError("throttled"))
        )
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        # Fail-soft: the follow-up stays queued and flushes at end of turn.
        await hook.inject_pending_steering(_after_tools(message))

        assert len(message["content"]) == 1


class TestCommitOnAppend:
    @pytest.mark.asyncio
    async def test_injection_alone_does_not_consume_the_inbox(
        self, monkeypatch, session_manager
    ):
        clear = AsyncMock(return_value=True)
        _patch_lease(monkeypatch, peek=[{"id": "e1", "text": "hi"}], clear=clear)
        hook = SteeringHook(session_manager)

        await hook.inject_pending_steering(_after_tools(_tool_result_message()))

        # This is the interrupt path in miniature: the message the hook mutated
        # is never appended, so the entry must survive for re-delivery.
        clear.assert_not_awaited()
        assert hook.drain_applied() == []

    @pytest.mark.asyncio
    async def test_entry_is_cleared_once_its_message_reaches_history(
        self, monkeypatch, session_manager, lease
    ):
        clear = AsyncMock(return_value=True)
        _patch_lease(monkeypatch, peek=[{"id": "e1", "text": "hi"}], clear=clear)
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))
        await hook.commit_pending_steering(
            MessageAddedEvent(agent=MagicMock(), message=message)
        )

        clear.assert_awaited_once_with(lease, "e1")
        assert [e["id"] for e in hook.drain_applied()] == ["e1"]

    @pytest.mark.asyncio
    async def test_a_different_message_does_not_ack_the_injection(
        self, monkeypatch, session_manager
    ):
        clear = AsyncMock(return_value=True)
        _patch_lease(monkeypatch, peek=[{"id": "e1", "text": "hi"}], clear=clear)
        hook = SteeringHook(session_manager)

        await hook.inject_pending_steering(_after_tools(_tool_result_message()))
        # Identity, not equality: an equal-looking message added by something
        # else must not consume the entry.
        await hook.commit_pending_steering(
            MessageAddedEvent(agent=MagicMock(), message=_tool_result_message())
        )

        clear.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_commit_is_ignored_without_a_pending_injection(
        self, monkeypatch, session_manager
    ):
        clear = AsyncMock(return_value=True)
        _patch_lease(monkeypatch, clear=clear)
        hook = SteeringHook(session_manager)

        # Every ordinary message of every ordinary turn takes this path.
        await hook.commit_pending_steering(
            MessageAddedEvent(agent=MagicMock(), message=_tool_result_message())
        )

        clear.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_commit_acks_only_once(self, monkeypatch, session_manager):
        clear = AsyncMock(return_value=True)
        _patch_lease(monkeypatch, peek=[{"id": "e1", "text": "hi"}], clear=clear)
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))
        event = MessageAddedEvent(agent=MagicMock(), message=message)
        await hook.commit_pending_steering(event)
        await hook.commit_pending_steering(event)

        assert clear.await_count == 1

    @pytest.mark.asyncio
    async def test_a_failed_clear_is_not_reported_as_applied(
        self, monkeypatch, session_manager
    ):
        _patch_lease(
            monkeypatch,
            peek=[{"id": "e1", "text": "hi"}],
            clear=AsyncMock(side_effect=RuntimeError("throttled")),
        )
        hook = SteeringHook(session_manager)
        message = _tool_result_message()

        await hook.inject_pending_steering(_after_tools(message))
        await hook.commit_pending_steering(
            MessageAddedEvent(agent=MagicMock(), message=message)
        )

        # The entry stays in the inbox and is re-injected at the next boundary;
        # the entry id makes the SPA's ack idempotent. Acking a clear that
        # never landed would be the unrecoverable direction.
        assert hook.drain_applied() == []


class TestSdkContract:
    """Canary for a ``strands-agents`` bump. See D2's SDK-boundary caveat.

    ``AfterToolsEvent._can_write`` allows only ``end_turn``, so the injection
    works by mutating the message dict in place. If a future SDK version
    deep-copies or freezes that message, these fail — and the documented escape
    hatch (interrupt/resume, per the spec's Risks) is the fallback design.
    """

    def test_after_tools_event_still_refuses_attribute_writes(self):
        event = _after_tools(_tool_result_message())
        with pytest.raises(Exception):
            event.message = {"role": "user", "content": []}

    def test_in_place_content_mutation_is_visible_to_the_caller(self):
        """The event holds the caller's message object, not a copy.

        This is the load-bearing assumption: ``event_loop`` builds
        ``tool_result_message``, hands it to the hook, and then appends *that
        same object* — so a block appended here reaches ``agent.messages``.
        """
        message = _tool_result_message()
        event = _after_tools(message)

        event.message["content"].append({"text": "injected"})

        assert message["content"][-1] == {"text": "injected"}
        assert event.message is message

    def test_message_added_event_carries_the_appended_object(self):
        """``_append_messages`` fires MessageAddedEvent with the same object.

        The ack path matches on identity, so a copy here would mean the inbox
        entry is never consumed and the text is re-injected every boundary.
        """
        message = _tool_result_message()
        event = MessageAddedEvent(agent=MagicMock(), message=message)
        assert event.message is message
